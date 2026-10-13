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

# Day 32 delivery summary

## 1. Что добавлено

Добавлен один общий AI Code Review Engine для трёх entrypoints: `/review` в существующем SSE-чате, bounded background API jobs и автономный `python -m app.review.cli` для GitHub Actions. Engine получает PR metadata и paginated changed files через GitHub REST API, группирует bounded diff, редактирует типовые secrets, использует base-SHA RAG и read-only MCP metadata, вызывает существующий `DeepSeekClient`, валидирует JSON через Pydantic, дедуплицирует findings и проверяет line references по добавленным строкам hunk.

Обязательные категории: `potential_bug`, `architecture_issue`, `recommendation`; severity: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFO`. Finding содержит стабильный ID, title, description, impact, evidence, file/range, suggested fix, backend-generated permalink и confidence. Findings ниже configurable confidence `0.60` отбрасываются. Бинарные/недоступные/вышедшие за budget файлы фиксируются в `unreviewed_files`, а полнота становится `partial`.

## 2. Automatic review

Workflow установлен в отдельной ветке `aurelSpa/feature/ai-code-review` и открыт как PR:

- PR: `https://github.com/SlavaM1/aurelSpa/pull/1`.
- Workflow branch HEAD: `5de94faa2fba1ebc18b55e4927cce5c3e5cf4fea`.
- Pinned AI Helper: `41018c3f41ec51fc59f5188f7d9100b955ce19d0`.
- Events: `opened`, `synchronize`, `reopened`, `ready_for_review`.
- Permissions: `contents: read`, `pull-requests: write`.
- Draft и fork PR пропускаются; `pull_request_target` не используется.
- Checkout выполняется только для trusted base SHA; код PR не запускается.
- `actions/checkout` и `actions/setup-python` закреплены commit SHA.
- Secret `DEEPSEEK_API_KEY` установлен через GitHub Actions secrets без вывода значения.

Первый реальный run на старом AI Helper SHA выявил нехватку output budget для reasoning-модели и завершился failure. После добавления JSON mode, JSON envelope parsing, отдельного `REVIEW_MAX_OUTPUT_TOKENS=8192` и failed-state persistence повторный run `38401257885` прошёл успешно за 1m45s. Ручной rerun того же run также успешно завершился за 1m54s и обновил существующий comment `6146109809`; marker-comments осталось ровно один.

Workflow пока не merged. Он проверенно работает в integration PR, но станет штатно активен для последующих PR в `main` только после merge PR #1. Автоматический merge не выполнялся.

## 3. Manual review и follow-up

Поддерживаются `/review 1` и полный URL разрешённого `SlavaM1/aurelSpa`. Live smoke PR #1 прошёл цепочку GitHub API -> diff -> RAG -> MCP -> DeepSeek -> Pydantic -> SQLite -> SSE/chat. Успешный manual run проанализировал 1/1 файл, использовал `deepseek-flash`, 21 729 provider tokens и 88 870 ms. Предыдущие два невалидных JSON smoke attempts сохранены как failed run, а не скрыты.

Следующий вопрос в том же conversation успешно получил `active_code_review` с review ID, summary и findings и ответил о первой проблеме без повторной передачи полного diff. Manual review не создал GitHub comment; первый comment появился только в Actions run.

## 4. RAG и MCP

Review RAG переиспользует Day 31 `HybridRetriever`. Перед retrieval проверяется `index_states.status=ready` и точное совпадение `commit_sha` с base SHA PR. Для live PR #1 использован индекс `main@eff6cb4dd0f6a30797da4380548e30c7a63be9ba`, 147 files / 631 chunks, совпадающий с PR base. В CI тот же `RagIndexer` строит отдельную временную SQLite базу по trusted base checkout; глобальный локальный индекс не перезаписывается.

Diff всегда приходит из GitHub PR API, а не из локального `git_diff`. MCP используется по назначению: `git_repo_info` подтверждает repository/branch/commit trusted checkout. Arbitrary shell и write Git tools не добавлялись.

## 5. GitHub sync

Comment содержит marker `ai-helper-code-review:v1` и versioned base64 JSON metadata. Publisher ищет marker только среди comments текущего authenticated actor и обновляет один собственный comment. Чужие comments не редактируются.

`POST /api/reviews/sync` импортировал Actions report: первый sync `synced_reviews=1`, повторный `0`. Для rerun на том же head сравнивается `generated_at`: более новая metadata обновляет существующий review, неизменная остаётся идемпотентной. После финального sync PR #1 имеет status `completed`, reviewed/current head `5de94faa...`, 1 finding, `deepseek-flash`, 9 216 tokens и 32 373 ms из обновлённого rerun report. Первый successful report до rerun содержал 2 findings, 9 984 tokens и 36 399 ms.

## 6. SQLite

Добавлены таблицы:

- `pull_requests`;
- `code_reviews`;
- `review_findings`;
- `review_runs`;
- `review_sources`;
- `conversation_reviews`.

Существующие `conversations`, `messages`, RAG tables и named volume не удалялись. На проверке после нескольких force-recreate/restart сохранены 5 conversations, 22 messages, review history и RAG index. Новые таблицы добавляются отдельно через SQLAlchemy `create_all`; существующие таблицы не изменяются.

## 7. Backend API и UI

Добавлены endpoints `/api/pull-requests`, `/api/pull-requests/{number}`, `/api/pull-requests/{number}/reviews`, background `POST .../review`, `/api/reviews`, `/api/reviews/{id}`, `/findings`, `/discuss` и `/api/reviews/sync`. Для одного PR/head запрещены дублирующиеся active jobs; semaphore ограничивает параллелизм.

В существующем React SPA добавлена навигация **Code Reviews**, PR list, refresh/sync, run/re-run, GitHub link, status polling, outdated state, model/duration/finding counts, report detail, severity/category filters, RAG sources и **Discuss in Chat**. Отдельное приложение и новый frontend stack не создавались.

## 8. DeepSeek

Provider live вернул `deepseek-flash` и `deepseek-v4-pro`. Day 32 structured review и follow-up проверены live на `deepseek-flash`. Для structured review включён `response_format=json_object`; hidden reasoning не читается и не сохраняется. Отдельный review output budget равен 8192. `deepseek-v4-pro` в Day 32 review не запускался; его live chat проверка относится к Day 31.

## 9. Tests

Финальная ожидаемая матрица после последнего sync fix:

- Backend pytest: 21 tests, включая 10 review tests.
- Ruff: без ошибок.
- Frontend Vitest: 16 tests.
- Frontend typecheck и production build: успешно.
- GitHub Actions: один initial failure, затем два successful executions на run `38401257885` (original + rerun).
- Live manual `/review 1`: успешно после исправления output budget.
- Live follow-up: успешно, review context сохранён в conversation.
- Live GitHub sync и повторная идемпотентная sync: успешно.

Точные финальные counts подтверждаются последним test run перед delivery; если они отличаются, приоритет имеет финальный ответ.

## 10. Docker

Backend и frontend images пересобраны, services force-recreated без `down -v`. Рабочие адреса сохранены:

- UI: `http://localhost:3010`.
- API/Swagger: `http://localhost:8010`, `http://localhost:8010/docs`.

Health проверен напрямую и через nginx; backend version `2.0.0`, DeepSeek configured, repository available. MCP, RAG, models, PR API и SQLite tables проверены реальными HTTP/database responses.

## 11. Ограничения

- PR #1 должен быть merged вручную, чтобы workflow стал частью `main` и штатно применялся к следующим PR.
- Fork PR намеренно пропускаются из-за отсутствия secrets/write permissions.
- Python dependency ranges ограничены major versions, но lockfile с hashes для Actions пока отсутствует.
- GitHub Actions вывел deprecation warning для Node 20 внутри закреплённых revisions `checkout@v4`/`setup-python@v5`; run успешен, но pins потребуется обновить до проверенных новых major revisions.
- SQLite schema по-прежнему не использует Alembic; Day 32 добавляет только новые таблицы и не изменяет существующие columns.
- Нет browser E2E/screenshots; UI проверен component tests, typecheck, production build, nginx и live API.
- AI review остаётся advisory и не заменяет тесты, security tooling и human review.

## 12. Git

- `ai-helper` branch: `dev/day32`.
- Review engine commit, закреплённый в workflow: `41018c3f41ec51fc59f5188f7d9100b955ce19d0`.
- Финальный documentation/sync-fix commit и remote HEAD указываются в delivery response после push.
- `aurelSpa` branch: `feature/ai-code-review`.
- `aurelSpa` workflow commit: `5de94faa2fba1ebc18b55e4927cce5c3e5cf4fea`.
- PR: `https://github.com/SlavaM1/aurelSpa/pull/1`, state open, merge не выполнялся.
