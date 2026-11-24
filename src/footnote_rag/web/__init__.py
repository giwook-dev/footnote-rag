"""빌드 없는 웹 화면(``static/index.html``, ``app.js``, ``style.css``).

``main.create_app``이 ``static/``을 ``/ui``에 마운트한다. 화면은 기존 HTTP API만 호출하므로
권한 검사와 조직 격리는 서버가 그대로 적용한다.
"""
