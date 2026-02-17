# llm-based-corporate-fillings

구조:

1. 공시 자료 받아오는 함수
2. 공시 자료 읽는 함수
3. 공시 자료 원문 데이터를 llm에 넣는 함수
4. llm 프롬프트는 어떻게 관리할까
5. 그래서 공시 자료를 어떻게 정리하고 싶은데?
   5.1. 세 가지 계층으로 정리
   5.2. raw 데이터, 정규화 데이터, 분석 데이터

---

## Server / Client 분리 실행

### 1) 서버 실행 (`server/`)
```bash
pip install -r server/requirements.txt
python server/run.py
```

- API: `http://127.0.0.1:8000`
- 사용 env: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_TABLE`

### 2) 프론트 실행 (`client/`)
```bash
cd client
npm install
npm run dev
```

- UI: `http://127.0.0.1:5173`
- Vite proxy로 `/api`는 서버(`:8000`)로 전달됨
