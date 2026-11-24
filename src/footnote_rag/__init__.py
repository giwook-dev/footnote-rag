"""footnote-rag 패키지 루트.

역할: 패키지 버전(``__version__``)만 정의한다. 실제 기능은 하위 패키지에 있다.
호출 관계: ``main.create_app``이 ``__version__``을 FastAPI 앱 버전으로 쓴다.
학습 문서: docs/study/01-big-picture.md (모듈 지도)

footnote-rag: footnoted RAG answers over DART audit reports.
"""

__version__ = "0.1.0"
