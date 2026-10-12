# Day 31 delivery summary

## 1. Что реализовано

Developer AI Assistant реализован как рабочее локальное приложение: React-чат, FastAPI REST/SSE API, DeepSeek agent, SQLite history/metrics, гибридный RAG по реальному `aurelSpa`, официальный MCP client/server для read-only Git tools, ручная и автоматическая переиндексация, Docker Compose и persistent volumes.

Поддерживаются создание, открытие, переименование и удаление диалогов; Markdown/code rendering; динамический выбор модели; context window 8K/16K/32K/64K и custom 4096-131072; sources, tools, usage, summary memory и aggregate statistics.

## 2. Технологии

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite WAL, Uvicorn.
- AI: OpenAI-compatible async client, DeepSeek streaming API.
- Embeddings: FastEmbed/ONNX, `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`.
- Search: normalized cosine similarity + SQLite FTS5/keyword fallback + metadata ranking.
- MCP: официальный Python SDK `mcp`, FastMCP, stdio transport.
- Frontend: React 19, TypeScript, Vite, nginx, react-markdown/GFM, highlight.js.
- Infrastructure: Docker Compose, named volumes, read-only bind mount target repository.

## 3. RAG

Scanner принимает Markdown/TXT/JSON/YAML/SQL/XML/properties/HTML и основные языки исходников, ограничивает размер/число файлов, не проходит symlink и исключает `.git`, dependencies, build output, static/vendor/uploads, binary и `.env`/secret paths. Перед chunking типовые credentials redacted с сохранением строк.

Structural chunker сохраняет repository, branch, commit SHA, path, title, section, chunk ID, start/end lines и content hash. Индексация инкрементальна по hash и Git revision, удаляет исчезнувшие документы. Retrieval объединяет semantic/keyword scores, учитывает архитектурные path hints, дедуплицирует пересечения и ограничивает top K/threshold/token budget. Источники формируют GitHub permalink по commit SHA.

Фактически проиндексирован `SlavaM1/aurelSpa`: **147 файлов, 631 chunk**, `main`, `eff6cb4dd0f6a30797da4380548e30c7a63be9ba`.

## 4. MCP tools

- `git_current_branch`: branch, HEAD SHA, repository.
- `git_list_files`: tracked files, safe prefix, bounded limit.
- `git_status`: porcelain status.
- `git_diff`: bounded unstaged diff, safe optional path.
- `git_repo_info`: name/path/remote/branch/commit/dirty without credentials.

MCP проверен реальным official stdio protocol и live `aurelSpa`. Произвольного shell и write tools нет.

## 5. DeepSeek

Provider `GET /models` реально вернул `deepseek-flash` и `deepseek-v4-pro`. Обе модели проверены live chat completion через приложение. Последняя проверка `deepseek-v4-pro` вернула фактический usage: 1987 input, 18 output, 2005 total tokens. При недоступном model endpoint UI получает явно помеченный fallback, а не фиктивный provider result.

## 6. SQLite history

В SQLite сохраняются conversations, settings, messages, summary boundary, provider/estimated usage, latency, tools, sources, RAG documents/chunks и index state. Включены WAL, foreign keys и busy timeout. Удаление conversation каскадно удаляет messages, но не общий индекс. Named volume `assistant_data` сохраняет данные между restart.

Live smoke: 6 сообщений пережили restart backend. Отдельный delete smoke вернул `204`, последующее чтение удаленного диалога — `404`. Тестовые диалоги после проверки удалены, чтобы UI открывался чистым.

## 7. Summary memory

Считаются только user messages. На 10-м, 20-м и каждом следующем кратном 10 запросе LLM обновляет накопительную структурированную память. Последние три пары остаются raw; граница хранится как message ID. Summary usage записывается отдельно в `summary_tokens`. Ошибка summary не прерывает основной chat.

Unit test прогнал 20 user requests с mocked provider и подтвердил ровно два summary update и отдельный учет токенов. Реальный API provider проверен отдельно; 20 платных live-запросов только ради boundary test не выполнялись.

## 8. Тесты

- Backend: **11 passed** в Python 3.12 Docker image.
- Ruff: `All checks passed`.
- RAG tests: scanner/redaction/chunk metadata, hybrid retrieval, permalink, incremental update/delete.
- MCP tests: official protocol, real temp Git branch/SHA/tools, unsafe paths/limits.
- Agent tests: context budget, summary on request 10 and 20.
- CRUD tests: conversation/settings/messages/count/usage/cascade delete.
- Frontend: **9 passed** (Vitest/Testing Library), включая API/SSE, sidebar CRUD/settings и stats.
- Frontend typecheck и production Vite build: успешно.

## 9. Docker

Production images backend/frontend собраны. Compose config валиден. Оба контейнера healthy, frontend nginx proxy проверен через `/api/health`. Volumes `assistant_data` и `embedding_cache` созданы; target repository смонтирован `/repos/aurelSpa:ro`.

Порты `3000` и `8000` были заняты ранее запущенными чужими локальными проектами, поэтому рабочий экземпляр оставлен на:

- UI: `http://localhost:3010`
- API/Swagger: `http://localhost:8010`, `http://localhost:8010/docs`

Compose defaults остаются 3000/8000 и на свободной машине дают требуемый `http://localhost:3000`.

## 10. Реально проверено на aurelSpa

- GitHub auth и `git ls-remote`.
- Branch `main`, clean worktree, HEAD `eff6cb4dd0f6a30797da4380548e30c7a63be9ba`.
- Java 17/Spring Boot 2.6.3, Maven, MVC/Thymeleaf, Security, Data JPA/REST, MySQL, Flyway migrations V1-V5.
- Индексация Java, `pom.xml`, properties, SQL и templates с secret redaction.
- Live MCP branch/repository metadata.
- Live RAG retrieval с permalink на entry point, `pom.xml`, application config и controller/service files.
- `/help` MCP-only и RAG + MCP SSE scenarios с фактическим provider usage.

`aurelSpa` не изменялся и не пушился.

## 11. Ограничения

- Agent выбирает RAG/MCP через прозрачный bounded intent router, а не передает модели право выбирать произвольные tools.
- Локальная token estimate консервативна; точные значения берутся из provider usage, когда доступны.
- Secret redaction эвристический и не заменяет dedicated secret scanner; `git_diff` не redacted.
- Summary boundaries проверены unit/integration mock; отдельный live 20-request summary smoke не выполнялся.
- Нет browser automation screenshot/E2E; UI production build, component tests, nginx и API proxy проверены.
- Приложение single-user без auth и намеренно публикуется только на localhost.

## 12. Git

- Branch: `dev/day31`.
- Implementation commit SHA: `8510f8d80a568b79f7666b6600baa1029d2bd6ac`.
- Delivery summary добавлен следующим commit; точный remote HEAD приведен в финальном отчете и проверен командой `git ls-remote origin refs/heads/dev/day31`.

SHA самого commit, содержащего этот файл, невозможно надежно включить в его содержимое до вычисления Git object ID без создания следующего commit. Финальный SHA поэтому фиксируется в delivery response и remote branch verification.
