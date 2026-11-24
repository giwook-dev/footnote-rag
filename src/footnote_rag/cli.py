"""Command line tools.

python -m footnote_rag.cli seed --org "Demo" --admin-email a@example.com --admin-password ...
python -m footnote_rag.cli dart-find --stock-codes 005930,035420,005380 --from 20260101 --to 20260531
python -m footnote_rag.cli dart-fetch --rcept-no 20260310000123   # download + print outline
python -m footnote_rag.cli dart-import --org-admin-email a@example.com --rcept-no 2026...,2026...
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from footnote_rag.config import get_settings
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
    from footnote_rag.dart.corp_codes import CorpCodeCache
    from footnote_rag.jobs.handlers import dart_cache_dir
    from footnote_rag.providers import make_dart_client

    s = get_settings()
    client = make_dart_client(s)
    cache_path = dart_cache_dir(s.dart_dir, client) / "corp_codes.json"
    cache = CorpCodeCache(client, cache_path, s.dart_corp_code_ttl_days)
    print(f"# OpenDART mode: {s.dart_mode}", file=sys.stderr)
    print("corp_name\tstock_code\treport_nm\trcept_no\trcept_dt")
    found = 0
    for stock_code in [c.strip() for c in args.stock_codes.split(",") if c.strip()]:
        try:
            corp = await cache.by_stock_code(stock_code)
        except KeyError:
            print(f"# stock_code {stock_code} not found in corpCode list", file=sys.stderr)
            continue
        reports = await client.list_reports(
            corp_code=corp.corp_code, bgn_de=args.date_from, end_de=args.date_to, detail=args.detail
        )
        for r in reports:
            found += 1
            print(f"{r.corp_name}\t{stock_code}\t{r.report_nm}\t{r.rcept_no}\t{r.rcept_dt}")
    print(f"# {found} report(s) (pblntf_detail_ty={args.detail})", file=sys.stderr)
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
        print(f"== {rcept_no}: {parsed.title} ({len(parsed.sections)} sections)")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="footnote_rag.cli")
    sub = p.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="create the first organization and its admin")
    seed.add_argument("--org", required=True)
    seed.add_argument("--admin-email", required=True)
    seed.add_argument("--admin-password", required=True)

    find = sub.add_parser("dart-find", help="list audit reports for stock codes")
    find.add_argument("--stock-codes", required=True)
    find.add_argument("--from", dest="date_from", required=True)
    find.add_argument("--to", dest="date_to", required=True)
    find.add_argument("--detail", default="F001", help="F001 audit report, F002 consolidated")

    fetch = sub.add_parser("dart-fetch", help="download a filing into data/dart and print outline")
    fetch.add_argument("--rcept-no", required=True)

    imp = sub.add_parser("dart-import", help="queue DART import jobs (same service as the API)")
    imp.add_argument("--org-admin-email", required=True)
    imp.add_argument("--rcept-no", required=True)
    return p


COMMANDS = {
    "seed": cmd_seed,
    "dart-find": cmd_dart_find,
    "dart-fetch": cmd_dart_fetch,
    "dart-import": cmd_dart_import,
}


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    s = get_settings()
    configure_logging(s.log_level if s.log_level != "INFO" else "WARNING", s.log_json)
    args = build_parser().parse_args(argv)
    return asyncio.run(COMMANDS[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
