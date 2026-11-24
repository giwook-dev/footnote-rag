"""명령줄 도구(seed, dart-find, dart-fetch, dart-import, dart-reprocess).

역할: 첫 조직·관리자 생성, 접수번호 찾기(기본 A001 사업보고서), 원문 ZIP 받아 목차 출력(파서 확인용),
가져오기 작업 등록, 이미 가져온 문서의 재처리 작업 등록(파서 변경 뒤 다시 파싱).
주요 요소: ``cmd_seed``, ``cmd_dart_find``, ``cmd_dart_fetch``, ``cmd_dart_import``, ``cmd_dart_reprocess``,
``build_parser``.
호출 관계: ``orgs.service``, ``dart.service.find_reports``(``GET /dart/reports``와 공유), ``jobs.handlers.load_dart_zip``,
``documents.service.request_dart_import``·``request_reprocess``(API와 같은 서비스 함수)를 호출한다.
학습 문서: docs/study/07-dart-integration.md (dart-fetch로 실제 구조 확인)

Command line tools.

python -m footnote_rag.cli seed --org "Demo" --admin-email a@example.com --admin-password ...
python -m footnote_rag.cli dart-find --stock-codes 005930,035420,005380 --from 20260101 --to 20260531
python -m footnote_rag.cli dart-fetch --rcept-no 20260310000123   # download + print outline
python -m footnote_rag.cli dart-import --org-admin-email a@example.com --rcept-no 2026...,2026...
python -m footnote_rag.cli dart-reprocess --org-admin-email a@example.com (--rcept-no 2026...,... | --all)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from footnote_rag.config import get_settings
from footnote_rag.dart.client import DEFAULT_DETAIL, REPORT_DETAILS
from footnote_rag.observability import configure_logging


def _ensure_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


async def cmd_seed(args: argparse.Namespace) -> int:
    from footnote_rag.db.session import dispose_engine, get_sessionmaker
    from footnote_rag.orgs.service import EmailTakenError, seed_org

    async with get_sessionmaker()() as session:
        try:
            org, admin = await seed_org(
                session,
                org_name=args.org,
                admin_email=args.admin_email,
                admin_password=args.admin_password,
            )
        except EmailTakenError:
            print(f"user {args.admin_email} already exists", file=sys.stderr)
            await dispose_engine()
            return 1
    print(f"created org {org.id} ({org.name}) with admin {admin.email}")
    await dispose_engine()
    return 0


async def cmd_dart_find(args: argparse.Namespace) -> int:
    from footnote_rag.dart.service import find_reports

    s = get_settings()
    print(f"# OpenDART mode: {s.dart_mode}", file=sys.stderr)
    print("corp_name\tstock_code\treport_nm\trcept_no\trcept_dt")
    stock_codes = [c.strip() for c in args.stock_codes.split(",") if c.strip()]
    reports, not_found, _mode = await find_reports(
        s, stock_codes, args.date_from, args.date_to, args.detail
    )
    for stock_code in not_found:
        print(f"# stock_code {stock_code} not found in corpCode list", file=sys.stderr)
    for r in reports:
        print(
            f"{r['corp_name']}\t{r['stock_code']}\t{r['report_nm']}\t{r['rcept_no']}\t{r['rcept_dt']}"
        )
    print(f"# {len(reports)} report(s) (pblntf_detail_ty={args.detail})", file=sys.stderr)
    return 0


async def cmd_dart_fetch(args: argparse.Namespace) -> int:
    from footnote_rag.dart.parser import parse_document_zip
    from footnote_rag.jobs.handlers import dart_cache_dir, load_dart_zip
    from footnote_rag.providers import make_dart_client

    s = get_settings()
    client = make_dart_client(s)
    for rcept_no in [r.strip() for r in args.rcept_no.split(",") if r.strip()]:
        content = await load_dart_zip(client, dart_cache_dir(s.dart_dir, client), rcept_no)
        parsed = parse_document_zip(content, rcept_no)
        fixes = parsed.sanitized
        print(
            f"== {rcept_no}: {parsed.title} ({len(parsed.sections)} sections) "
            f"(sanitized &:{fixes.get('bare_amp', 0)} <:{fixes.get('bare_lt', 0)} "
            f"attr:{fixes.get('bad_attr_quote', 0)} tag:{fixes.get('pseudo_tag', 0)})"
            + (" TRUNCATED" if parsed.truncated else "")
        )
        for section in parsed.sections:
            print(f"  [{len(section.text):>6} chars] {section.path}")
    return 0


async def cmd_dart_import(args: argparse.Namespace) -> int:
    from footnote_rag.db.session import dispose_engine, get_sessionmaker
    from footnote_rag.documents.service import InvalidRceptNoError, request_dart_import
    from footnote_rag.orgs.service import get_user_by_email

    rc = 0
    async with get_sessionmaker()() as session:
        user = await get_user_by_email(session, args.org_admin_email)
        if user is None or user.role != "admin":
            print("admin user not found", file=sys.stderr)
            await dispose_engine()
            return 1
        for rcept_no in [r.strip() for r in args.rcept_no.split(",") if r.strip()]:
            try:
                result = await request_dart_import(session, user=user, rcept_no=rcept_no)
            except InvalidRceptNoError as exc:
                print(f"{rcept_no}\tinvalid: {exc}", file=sys.stderr)
                rc = 1
                continue
            state = "existing" if result.existing else "queued"
            print(f"{rcept_no}\t{state}\tdocument={result.document.id}\tjob={result.job_id}")
    await dispose_engine()
    return rc


async def cmd_dart_reprocess(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from footnote_rag.db.models import Document
    from footnote_rag.db.session import dispose_engine, get_sessionmaker
    from footnote_rag.documents.service import (
        DocumentBusyError,
        DocumentNotFoundError,
        request_reprocess,
    )
    from footnote_rag.orgs.service import get_user_by_email

    rc = 0
    async with get_sessionmaker()() as session:
        user = await get_user_by_email(session, args.org_admin_email)
        if user is None or user.role != "admin":
            print("admin user not found", file=sys.stderr)
            await dispose_engine()
            return 1
        org_id = user.org_id
        query = select(Document.source_ref, Document.id).where(
            Document.org_id == org_id, Document.source == "dart"
        )
        if args.all:
            rows = list((await session.execute(query.order_by(Document.created_at))).all())
            targets = [(r.source_ref, r.id) for r in rows]
        else:
            wanted = [r.strip() for r in args.rcept_no.split(",") if r.strip()]
            found = dict(
                (await session.execute(query.where(Document.source_ref.in_(wanted)))).all()
            )
            targets = [(r, found.get(r)) for r in wanted]
        await session.commit()  # end the read-only transaction; keeps `user` loaded
        for rcept_no, document_id in targets:
            if document_id is None:
                print(f"{rcept_no}	not found", file=sys.stderr)
                rc = 1
                continue
            try:
                result = await request_reprocess(session, user=user, document_id=document_id)
            except DocumentNotFoundError:
                print(f"{rcept_no}	not found", file=sys.stderr)
                rc = 1
                continue
            except DocumentBusyError as exc:
                print(f"{rcept_no}	skipped: document is {exc}", file=sys.stderr)
                rc = 1
                continue
            print(f"{rcept_no}	queued	document={document_id}	job={result.job_id}")
        if not targets:
            print("no DART documents to reprocess", file=sys.stderr)
    await dispose_engine()
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="footnote_rag.cli")
    sub = p.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="create the first organization and its admin")
    seed.add_argument("--org", required=True)
    seed.add_argument("--admin-email", required=True)
    seed.add_argument("--admin-password", required=True)

    find = sub.add_parser("dart-find", help="list annual/audit reports for stock codes")
    find.add_argument("--stock-codes", required=True)
    find.add_argument("--from", dest="date_from", required=True)
    find.add_argument("--to", dest="date_to", required=True)
    find.add_argument(
        "--detail",
        default=DEFAULT_DETAIL,
        choices=sorted(REPORT_DETAILS),
        help=(
            "pblntf_detail_ty (default A001): A001 annual report — listed companies include their "
            "audited statements and notes here; F001 audit report; F002 consolidated audit report"
        ),
    )

    fetch = sub.add_parser("dart-fetch", help="download a filing into data/dart and print outline")
    fetch.add_argument("--rcept-no", required=True)

    imp = sub.add_parser("dart-import", help="queue DART import jobs (same service as the API)")
    imp.add_argument("--org-admin-email", required=True)
    imp.add_argument("--rcept-no", required=True)

    rep = sub.add_parser(
        "dart-reprocess",
        help="re-parse already imported DART documents (same service as the API, cached ZIPs)",
    )
    rep.add_argument("--org-admin-email", required=True)
    target = rep.add_mutually_exclusive_group(required=True)
    target.add_argument("--rcept-no", help="comma separated receipt numbers")
    target.add_argument("--all", action="store_true", help="every DART document of the org")
    return p


COMMANDS = {
    "seed": cmd_seed,
    "dart-find": cmd_dart_find,
    "dart-fetch": cmd_dart_fetch,
    "dart-import": cmd_dart_import,
    "dart-reprocess": cmd_dart_reprocess,
}


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    s = get_settings()
    configure_logging(s.log_level if s.log_level != "INFO" else "WARNING", s.log_json)
    args = build_parser().parse_args(argv)
    return asyncio.run(COMMANDS[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
