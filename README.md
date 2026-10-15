# Developer AI Assistant

Локальный помощник разработчика для исследования репозитория `aurelSpa`. Приложение хранит диалоги, дополняет запросы найденными фрагментами исходников, получает состояние Git через read-only MCP-сервер и передает собранный контекст в OpenAI-compatible API DeepSeek.

Целевой репозиторий не является частью этого проекта. По умолчанию ожидается соседний каталог `../aurelSpa`, который монтируется в backend только для чтения.

## Возможности

- Диалоги с отдельным выбором модели и окна контекста от 4096 до 131072 токенов.
- Потоковые ответы DeepSeek через Server-Sent Events (SSE).
- Гибридный RAG-поиск по исходникам: embeddings, cosine similarity и SQLite FTS5.
- Ссылки на исходники с путем, диапазоном строк и commit, если remote указывает на GitHub.
- Read-only Git-инструменты по официальному MCP-протоколу поверх stdio.
- Сохранение сообщений, usage, источников, вызовов tools и временных метрик.
- Периодическая summary-память длинного диалога.
- React-интерфейс с Markdown/GFM, подсветкой кода, историей диалогов, статистикой и ручной переиндексацией.
- Project File Agent: автономный поиск, чтение и реальные изменения в изолированных Git-копиях; [Day 34](#day-34--autonomous-project-file-agent).

## Архитектура

```text
браузер
  -> http://localhost:3000
  -> React 19 SPA, собранный Vite и отданный nginx
  -> /api/* reverse proxy
  -> FastAPI :8000
       -> DeepSeek OpenAI-compatible API
       -> SQLite WAL /data/assistant.db
       -> RAG: multilingual MiniLM ONNX + cosine + FTS5
       -> официальный MCP client
            -> Python FastMCP server по stdio
                 -> ограниченный набор read-only Git-команд
                 -> /repos/aurelSpa (bind mount :ro)
```

Backend создает таблицы SQLAlchemy при старте. Если `AI_HELPER_AUTO_INDEX=true` и репозиторий доступен, индексация запускается в фоновом потоке. Frontend обращается к относительному `/api`; nginx отключает proxy buffering, чтобы SSE-ответы приходили по мере генерации.

### Стек помощника

| Область | Реализация |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn, Pydantic Settings |
| Данные | SQLAlchemy 2, SQLite с `foreign_keys=ON`, `busy_timeout=5000`, WAL |
| LLM | `openai.AsyncOpenAI`, DeepSeek/OpenAI-compatible endpoint |
| RAG | FastEmbed/ONNX, `paraphrase-multilingual-MiniLM-L12-v2`, SQLite FTS5 |
| MCP | официальный Python SDK `mcp`, FastMCP, stdio transport |
| Frontend | React 19, TypeScript 5.8, Vite 7 |
| Markdown | `react-markdown`, GFM, `highlight.js` |
| Web runtime | nginx 1.27 Alpine |
| Тесты | pytest, pytest-asyncio, Vitest, Testing Library, jsdom |
| Контейнеры | Docker Compose, Python 3.12 slim, Node 22 Alpine |

## Подключенный aurelSpa

Проверенный снимок `../aurelSpa` на момент подготовки этого документа:

| Поле | Значение |
|---|---|
| Репозиторий | `https://github.com/SlavaM1/aurelSpa.git` |
| Ветка | `main` |
| Commit | `eff6cb4dd0f6a30797da4380548e30c7a63be9ba` |
| Состояние worktree | без изменений на момент проверки |
| Maven artifact | `com.example:InternetStore:0.0.1-SNAPSHOT` |

Это не зафиксированная зависимость: mount читает текущий соседний checkout, поэтому ветка, commit и dirty-состояние могут измениться. Фактические значения доступны через `GET /api/repository` и записываются в состояние RAG после индексации.

Реальный стек этого снимка определен по `pom.xml` и исходникам: Java 17, Spring Boot 2.6.3, Spring MVC, Thymeleaf, Spring Security, Spring Data JPA и REST, Spring Session JDBC, MySQL Connector/J, Flyway, Spring Mail, Hibernate Validator, Lombok, ModelMapper и WebJars Bootstrap 5.1.3. Это серверное MVC-приложение с Thymeleaf-шаблонами, статическими JavaScript/SCSS-ресурсами, JPA entities/repositories/services/controllers и SQL-миграциями `V1`-`V5`; отдельного Node frontend manifest в репозитории нет.

## RAG

### Сканирование и redaction

`RepoScanner` обходит репозиторий без перехода по symlink и индексирует UTF-8 файлы с расширениями `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.sql`, `.java`, `.js`, `.ts`, `.tsx`, `.py`, `.xml`, `.properties`, `.html`.

Ограничения по умолчанию:

| Ограничение | Значение |
|---|---:|
| Размер одного файла | 1 000 000 байт |
| Число файлов | 10 000 |
| Общий объем | 100 000 000 байт |

Исключаются `.git`, `node_modules`, виртуальные окружения, `build`, `dist`, `target`, `static`, `vendor`, `uploads`, каталоги `env`, `secrets`, `binaries`, файлы `.env*`, symlink, бинарные и не-UTF-8 файлы. Исключение `static` означает, что статические ресурсы `aurelSpa/src/main/resources/static` в индекс не попадают.

До chunking содержимое проходит эвристический secret-redaction. Маскируются типовые присваивания имен вроде `api_key`, `token`, `client_secret`, `password`, `private_key`, JSON-поля с такими именами, GitHub tokens, AWS access key IDs и PEM private keys. Переносы строк приватного ключа сохраняются, чтобы не смещать номера строк. SHA-256 документа вычисляется по исходным байтам для обнаружения изменений, но в `rag_chunks` сохраняется уже отредактированный текст.

Redaction не является полноценным secret scanner: нестандартные имена, разнесенные значения и произвольные credentials могут пройти фильтр. В проверенном commit `aurelSpa` файл `src/main/resources/application.properties` содержит непустое значение `spring.mail.password`; значение здесь намеренно не приводится. Его следует считать раскрытым, ротировать и удалить из текущего состояния и истории репозитория. Текущая RAG-эвристика распознает имя `password`, но исходный файл остается в checkout, а `git_diff` MCP не применяет RAG-redaction.

### Chunking и индекс

- Структурные границы определяются по Markdown headings, классам/interfaces/enums/records и определениям функций/методов.
- Максимальный размер чанка 1200 символов, overlap 200 символов; граница секции имеет приоритет над overlap.
- Для каждого чанка сохраняются путь, заголовок, секция, строки, hashes, repository, branch, commit и стабильный chunk ID.
- Embeddings строятся локальной multilingual-моделью `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` через FastEmbed/ONNX; 384-мерные векторы нормализуются.
- Индексация инкрементальная по SHA-256 исходного файла. Измененные документы заменяются, исчезнувшие удаляются только после полного сканирования, неизмененные не пересчитываются.
- После успешной индексации FTS5-таблица перестраивается целиком. Если SQLite собран без FTS5, используется лексический fallback в Python.

### Поиск

Поиск объединяет cosine score embeddings и keyword score с весами 0.7 и 0.3. По умолчанию используются `top_k=8`, минимальный итоговый score `0.08` и отдельный RAG-бюджет 5000 приблизительных токенов. Дубликаты текста удаляются, а превышающие бюджет чанки пропускаются.

GitHub permalink формируется только для распознанного GitHub remote и привязывается к commit, сохраненному во время индексации. Поэтому после обновления `aurelSpa` ссылки и ответы будут отражать старый снимок до переиндексации.

## MCP tools

Backend для каждого обращения запускает официальный MCP client/server session по stdio. Существующий read-only Git-сервер экспортирует следующие инструменты (отдельный файловый сервер описан в Day 34):

| Tool | Назначение и ограничения |
|---|---|
| `git_current_branch` | текущая ветка, HEAD commit и имя репозитория |
| `git_list_files` | tracked files, безопасный относительный prefix, limit 1-5000 |
| `git_status` | branch и porcelain-v1 status, вывод до 500 KB |
| `git_diff` | только unstaged diff, опциональный путь, limit 1-1 000 000 байт |
| `git_repo_info` | имя, путь, ветка, commit, очищенный remote и dirty flag |

Пути должны быть относительными, не могут содержать `..`, `.git`, NUL или переводы строк и дополнительно проверяются после `resolve`. Из окружения дочернего Git-процесса удаляются переменные, способные переопределить repository/config/diff; отключены credential helper, terminal prompt и optional locks. Remote URL очищается от userinfo, query и типовых token-параметров.

Git MCP не предоставляет произвольный shell и не содержит write-команд. Developer Assistant автоматически выбирает только `git_current_branch`, `git_status` или `git_diff`; `git_list_files` экспортируется Git MCP-сервером, но chat orchestrator его не выбирает, а `git_repo_info` используется endpoint `GET /api/repository`.

## Agent, context и summary

Сообщение сохраняется до вызова модели. Команда `/help` без текста возвращает локальную справку без DeepSeek. `/help вопрос` принудительно включает project mode; обычный вопрос включает его только по эвристическим словам о проекте, репозитории, коде, файлах или архитектуре. Выбор RAG и Git tool также эвристический, а не model-driven tool calling.

При расчете бюджета сначала резервируется место под system prompt, текущий запрос, summary и MCP-output. Затем отбирается свежая история с конца, а RAG получает оставшийся бюджет. В DeepSeek сообщения отправляются в таком порядке:

1. Системная инструкция, запрещающая считать RAG/MCP-данные инструкциями.
2. Текущая summary-память, если она есть.
3. RAG-чанки в пределах оставшегося после истории бюджета.
4. Результаты read-only MCP tools.
5. Отобранная свежая история после границы summary в хронологическом порядке.
6. Текущий пользовательский запрос.

Оценка токенов консервативная: примерно один токен на 3.2 символа. Из окна резервируются output budget до 2048 токенов и дополнительный системный запас. Это локальная оценка, а не provider usage. Если DeepSeek возвращает usage в финальном stream chunk, сохраняются его значения; иначе input/output оцениваются и сообщение помечается `tokens_estimated=true`.

Summary запускается после 10-го, 20-го, 30-го и последующих пользовательских сообщений. Модель обновляет структурированную память по еще не суммированной части истории, оставляя последние три пользовательских хода свежими. Граница хранится как message ID. При ошибке summary-вызова чат не прерывается, а память в этом цикле не обновляется.

Frontend отображает поток `delta`, источники, tools, metrics и финальное сообщение. Остановка генерации отменяет клиентский HTTP-запрос; уже сохраненное пользовательское сообщение при этом остается в базе.

## Конфигурация DeepSeek

Создайте локальный `.env` из `.env.example` и задайте ключ:

```bash
cp .env.example .env
```

| Переменная Compose | По умолчанию | Назначение |
|---|---|---|
| `DEEPSEEK_API_KEY` | пусто | обязательна для списка моделей, chat и summary |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible base URL |
| `DEFAULT_MODEL` | `deepseek-flash` | модель нового диалога |
| `AI_HELPER_AUTO_INDEX` | `true` | фоновая индексация при старте |
| `TARGET_REPO_HOST_PATH` | `../aurelSpa` | host/WSL path для read-only bind mount |
| `FRONTEND_PORT` | `3000` | localhost port интерфейса |
| `BACKEND_PORT` | `8000` | localhost port FastAPI |

Backend также понимает aliases `AI_HELPER_DEEPSEEK_API_KEY`, `AI_HELPER_DEEPSEEK_BASE_URL`, `AI_HELPER_DEFAULT_MODEL` и `AI_HELPER_TARGET_REPO_PATH`, но текущий Compose передает показанные в таблице имена и фиксирует container path как `/repos/aurelSpa`.

`GET /api/models` пытается получить модели у provider. При ошибке endpoint возвращает резервный список `deepseek-flash` и `deepseek-v4-pro` с полями `source=fallback` и `warning`. Наличие имени в резервном списке не подтверждает, что provider принимает эту модель; используемая модель сохраняется отдельно в каждом диалоге.

Не добавляйте `.env` в Git. Compose подставляет его значения в environment контейнера; файл не монтируется внутрь образа.

## WSL и mount репозитория

Рекомендуемая структура каталогов:

```text
workspace/
├── ai-helper/
└── aurelSpa/
```

Из корня `ai-helper` проверьте checkout:

```bash
test -d ../aurelSpa/.git && git -C ../aurelSpa rev-parse --short HEAD
```

В WSL включите интеграцию Docker Desktop для используемого distribution и запускайте Compose из WSL. Путь `../aurelSpa` разрешается Compose на стороне WSL/Linux и монтируется как `/repos/aurelSpa:ro`. Хранение обоих репозиториев в Linux filesystem WSL обычно быстрее и предсказуемее, чем bind mount из `/mnt/c`.

Для другой раскладки задайте Linux/WSL-путь, видимый Docker daemon:

```bash
TARGET_REPO_HOST_PATH=../aurelSpa docker compose config
```

В сгенерированной конфигурации bind mount должен иметь target `/repos/aurelSpa` и `read_only: true`.

## Запуск в Docker

Из корня проекта:

```bash
docker compose up --build
```

Compose публикует backend только на loopback: `http://localhost:8000`. Полезные адреса:

- API health: `http://localhost:8000/api/health`
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

- UI: `http://localhost:3000`

Frontend публикуется как `127.0.0.1:3000:80`; nginx внутри контейнера слушает порт `80` и проксирует `/api/*` на service name `backend:8000`.

Если порты заняты другим локальным проектом, задайте альтернативы без изменения container network:

```bash
FRONTEND_PORT=3010 BACKEND_PORT=8010 docker compose up -d
```

## API

| Метод | Path | Назначение |
|---|---|---|
| `GET` | `/api/health` | версия, наличие DeepSeek key и доступность mount |
| `GET` | `/api/models` | модели provider либо явно помеченный fallback |
| `GET` | `/api/conversations` | список до 100 последних диалогов |
| `POST` | `/api/conversations` | создать диалог |
| `GET` | `/api/conversations/{id}` | получить диалог |
| `PATCH` | `/api/conversations/{id}` | изменить title, model, context window или summary-поля |
| `DELETE` | `/api/conversations/{id}` | удалить диалог и его сообщения |
| `GET` | `/api/conversations/{id}/messages` | сообщения диалога, до 1000 |
| `POST` | `/api/conversations/{id}/chat` | SSE chat, body `{"message":"..."}` |
| `GET` | `/api/conversations/{id}/summary` | summary, счетчик user messages и дата обновления |
| `GET` | `/api/conversations/{id}/usage` | агрегированные токены, latency, RAG и estimates |
| `GET` | `/api/repository` | `git_repo_info` через MCP |
| `GET` | `/api/mcp/tools` | схемы доступных MCP tools |
| `GET` | `/api/rag/status` | status, число файлов/чанков, branch и commit |
| `POST` | `/api/rag/reindex` | фоновая инкрементальная переиндексация |

Пример создания диалога:

```bash
curl -sS http://localhost:8000/api/conversations \
  -H 'Content-Type: application/json' \
  -d '{"title":"Архитектура aurelSpa","context_window":16384}'
```

Пример SSE-запроса после подстановки ID:

```bash
curl -N http://localhost:8000/api/conversations/CONVERSATION_ID/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"/help Объясни архитектуру проекта"}'
```

## Обновление aurelSpa и reindex

Bind mount read-only только для контейнера: синхронизацию выполняйте в host/WSL checkout. Перед обновлением проверьте локальные изменения, затем используйте подходящую Git-операцию. Для чистой ветки `main`:

```bash
git -C ../aurelSpa status --short --branch
git -C ../aurelSpa pull --ff-only
git -C ../aurelSpa rev-parse HEAD
```

После обновления запустите reindex:

```bash
curl -sS -X POST http://localhost:8000/api/rag/reindex
curl -sS http://localhost:8000/api/rag/status
```

Первый запрос возвращает `202` и статус `indexing`; работа продолжается в backend. Повторяйте только `GET /api/rag/status`, пока status не станет `ready` или `error`. Параллельный reindex не запускается. Кнопка обновления у RAG-индикатора в UI вызывает тот же endpoint.

## Persistence

Compose использует два named volume:

| Volume | Содержимое |
|---|---|
| `assistant_data` | `/data/assistant.db` и SQLite WAL/SHM |
| `embedding_cache` | Hugging Face model cache в `/model-cache` |

В SQLite хранятся диалоги, сообщения, usage/metrics, tools/sources, summary, RAG documents/chunks/embeddings и состояние индекса. Удаление диалога каскадно удаляет только его сообщения; глобальный RAG-индекс остается. Удаление RAG document каскадно удаляет его chunks.

`docker compose down` сохраняет named volumes. `docker compose down -v` удаляет и историю диалогов, и индекс, и model cache; используйте эту команду только для намеренного полного сброса.

## Security

- Порты Compose привязаны к `127.0.0.1`, а CORS backend разрешает только `localhost:3000` и `127.0.0.1:3000`.
- В приложении нет аутентификации и авторизации. Не публикуйте backend или frontend в сеть без внешнего access control и TLS.
- API позволяет читать/изменять диалоги, запускать reindex и инициировать платные LLM-запросы; loopback binding является важной частью текущей модели угроз.
- `aurelSpa` монтируется `:ro`; MCP allowlist не содержит write Git commands и не выполняет пользовательские shell-команды.
- RAG-данные и MCP-output помечаются в prompt как недоверенные, но prompt injection из исходников нельзя считать полностью устраненным.
- В DeepSeek уходят пользовательский вопрос, выбранная история/summary, отредактированные RAG-чанки и выбранные MCP-результаты. Не подключайте чувствительный репозиторий без оценки политики внешнего provider.
- Secret-redaction применяется к RAG-тексту, но не исправляет исходный repository, не очищает Git history и не применяется к `git_diff`. Проверяйте репозиторий отдельным secret scanner.
- Remote credentials удаляются из `git_repo_info`, но секреты в именах файлов, status или diff могут попасть в MCP-output.
- Реальные API keys должны находиться только в локальном `.env` или secret manager, а не в Compose, README, image layers или Git.

## Troubleshooting Compose

### Backend unhealthy или frontend ждет backend

```bash
docker compose ps
docker compose logs backend
curl -v http://localhost:8000/api/health
```

Healthcheck делает запрос к `/api/health`. В ответе отдельно смотрите `deepseek_configured` и `repository_available`: общий `status=ok` не означает, что key настроен или mount доступен.

### Репозиторий недоступен, MCP отвечает 503

```bash
test -d ../aurelSpa/.git
docker compose config
docker compose exec backend test -d /repos/aurelSpa/.git
docker compose exec backend git -C /repos/aurelSpa rev-parse HEAD
```

Проверьте запуск из правильного каталога, WSL integration, разрешение `TARGET_REPO_HOST_PATH` и наличие `.git`. Пустой или ошибочный host path Docker может создать как каталог, после чего mount существует, но MCP отвергает его как не-Git repository.

### RAG долго остается в `indexing`

Первая индексация загружает embedding model в `embedding_cache` и может зависеть от сети, свободного места и доступности Hugging Face. Смотрите `docker compose logs backend` и `GET /api/rag/status`. Ошибка записывается в поле `message`; повторный `POST /api/rag/reindex` допустим после перехода в `error`.

### DeepSeek не отвечает

Проверьте наличие key внутри контейнера без вывода самого значения и запросите модели:

```bash
docker compose exec backend sh -c 'test -n "$DEEPSEEK_API_KEY"'
curl -sS http://localhost:8000/api/models
docker compose logs backend
```

Fallback от `/api/models` означает ошибку provider, а не успешную проверку модели. Для chat отсутствие key возвращается как SSE event `error`, потому что HTTP stream уже открыт.

### Порт занят

```bash
docker compose ps
docker compose down
```

Compose публикует только `127.0.0.1:8000` и `127.0.0.1:3000`. Освободите соответствующий host port или измените публикацию осознанно, не открывая сервис на `0.0.0.0` без защиты.

### SQLite locked или неожиданные данные

SQLite работает в WAL и ждет lock до 5 секунд. Не масштабируйте backend на несколько контейнеров с общей SQLite-базой. Для диагностики проверьте volume и логи; не удаляйте `assistant_data`, если нужна история.

## Results

Фактически выполнено в доступном WSL-окружении:

- Backend в Python 3.12 container: `11 passed`; `ruff check app tests` — без ошибок.
- Frontend: `9 passed`; `npm run typecheck` и `npm run build` — успешно.
- `docker compose config --quiet` и production build обоих images — успешно.
- Backend/frontend containers — healthy; health проверен напрямую и через nginx.
- Реальный DeepSeek `GET /models`, streaming chat с `deepseek-flash` и `deepseek-v4-pro` — успешно.
- Реальный `aurelSpa`: 147 файлов, 631 chunk, branch `main`, commit `eff6cb4dd0f6a30797da4380548e30c7a63be9ba`.
- MCP-only, RAG-only и совместный RAG + MCP `/help` сценарии — проверены через live SSE API.
- SQLite history пережила restart backend; live API cascade delete вернул `204`, последующее чтение — `404`.

Из-за уже занятых другими локальными проектами портов `3000` и `8000` проверенный экземпляр оставлен на `http://localhost:3010` и `http://localhost:8010`. При свободных портах Compose использует документированные defaults.

## Day 32 - AI Code Review

Day 32 добавляет один общий PR Review Engine для ручного запуска из чата, локального API и автономного GitHub Actions CLI. Engine получает metadata и все страницы changed files через GitHub REST API, рассматривает patch как недоверенные данные, применяет secret redaction, валидирует structured JSON через Pydantic и самостоятельно строит проверенные permalink. Строка finding сохраняется только тогда, когда она действительно входит в добавленные строки hunk; иначе остаётся привязка только к файлу.

### Потоки выполнения

```text
ручной: /review 15 -> FastAPI/SSE -> GitHub API -> Review Engine
                         -> base-SHA RAG + read-only MCP -> DeepSeek -> SQLite -> Chat/UI

автоматический: pull_request -> GitHub Actions -> python -m app.review.cli
                         -> тот же Review Engine -> один обновляемый PR comment

синхронизация: GitHub marker + versioned JSON metadata -> /api/reviews/sync
                         -> SQLite history -> Code Reviews UI
```

Ручное ревью сохраняется в `pull_requests`, `code_reviews`, `review_findings`, `review_runs`, `review_sources`, связывается с диалогом через `conversation_reviews` и публикует или обновляет marker-comment в GitHub PR. Автором комментария будет владелец локального `GITHUB_TOKEN`; повторный запуск обновляет его существующий marker-comment. Следующие сообщения используют summary и findings связанного review, а не повторно передают весь diff. Удаление существующих conversations/messages и перестроение базы не выполняется: новые таблицы создаются SQLAlchemy отдельно.

### RAG и MCP

Локальное ревью использует существующий `HybridRetriever` только если `index_states.commit_sha` точно совпадает с base SHA PR. Diff всегда берётся из GitHub PR, а не из локального `git diff`. При несовпадении snapshot Engine продолжает анализ diff, явно фиксирует предупреждение и не выдаёт устаревший RAG за base-контекст. CI checkout выполняется на доверенном base SHA и создаёт отдельную временную SQLite/RAG базу; код PR не запускается. Существующий read-only MCP `git_repo_info` подтверждает локальный Git snapshot и не предоставляет shell/write operations.

Большие PR делятся на bounded file groups. Лимит общего diff задаётся `AI_HELPER_REVIEW_MAX_DIFF_CHARS`; бинарные, недоступные и вышедшие за лимит файлы попадают в `unreviewed_files`, а `analysis_completeness` становится `partial`. Usage суммируется по фактическим ответам provider; если provider не вернул usage, точные tokens остаются `null`.

### Локальная конфигурация

Добавьте в локальный `.env`, не коммитя значения:

```dotenv
GITHUB_TOKEN=
GITHUB_REPOSITORY=SlavaM1/aurelSpa
REVIEW_MODEL=
REVIEW_MAX_OUTPUT_TOKENS=8192
REVIEW_MIN_CONFIDENCE=0.6
```

Для private repository контейнер не видит пользовательский `gh auth` автоматически. Передайте fine-grained token через `GITHUB_TOKEN` с read access к Contents/Pull requests. Write permission нужна только явной CLI-публикации; локальные API и `/review` её не используют. Не монтируйте в контейнер `~/.config/gh`, SSH keys или весь home directory.

### Ручное ревью

В существующем чате поддерживаются:

```text
/review 15
/review https://github.com/SlavaM1/aurelSpa/pull/15
```

SSE показывает этапы `Getting PR`, `Fetching diff`, `Retrieving project context`, `AI analysis`, `Finalizing review`. После ответа можно спрашивать о конкретном finding в том же диалоге. Вкладка **Code Reviews** получает PR, запускает/re-runs job, показывает состояния `not reviewed`, `pending`, `running`, `completed`, `failed`, `outdated`, фильтрует findings и создаёт связанный диалог через **Discuss in Chat**.

### Review API

| Метод | Path | Назначение |
|---|---|---|
| `GET` | `/api/pull-requests` | обновить open PR из GitHub и вернуть cached history |
| `GET` | `/api/pull-requests/{number}` | metadata и changed files PR |
| `GET` | `/api/pull-requests/{number}/reviews` | история запусков PR |
| `POST` | `/api/pull-requests/{number}/review` | bounded background review job |
| `GET` | `/api/reviews` | последние review runs |
| `GET` | `/api/reviews/{id}` | report, findings, sources и metrics |
| `GET` | `/api/reviews/{id}/findings` | findings отдельно |
| `POST` | `/api/reviews/{id}/discuss` | создать conversation, связанную с review |
| `POST` | `/api/reviews/sync` | идемпотентно импортировать Actions comments |

### CI entrypoint и GitHub Actions

CLI использует тот же Engine:

```bash
cd backend
python -m app.review.cli \
  --repo SlavaM1/aurelSpa \
  --pr 15 \
  --repository-path /path/to/trusted-base-checkout
```

Для smoke без GitHub comment добавьте `--no-publish`. В Actions задайте repository secret `DEEPSEEK_API_KEY`; встроенный `GITHUB_TOKEN` передаётся job с минимальными permissions. Workflow должен запускаться через `pull_request`, checkout только `github.event.pull_request.base.sha`, пропускать draft/fork PR и не использовать `pull_request_target`. Он становится активным для новых PR только после merge workflow в соответствующую base branch.

Comment содержит marker `ai-helper-code-review:v1` и versioned base64 JSON metadata. Publisher обновляет только marker-comment текущего authenticated actor, не трогает чужие комментарии и сохраняет один актуальный summary на PR. Локальная синхронизация создаёт отдельную historical запись на каждый reviewed head SHA.

### Тесты Day 32

```bash
docker compose run --rm --no-deps \
  -v "$PWD/backend:/src" -w /src backend \
  sh -c "pip install -e '.[test]' && python -m pytest && python -m ruff check app tests"

cd frontend
npm test -- --run
npm run typecheck
npm run build
```

Unit tests используют mock GitHub/DeepSeek: pagination, renamed/binary patches, changed-line validation, invalid structured output retry, empty diff, usage aggregation, persistence/conversation linkage, marker round-trip и безопасное обновление собственного comment. Реальный LLM нужен только для отдельного live smoke.

## Day 34 — Autonomous Project File Agent

Отдельная вкладка **File Agent** выполняет цели через настоящие файловые операции. Существующие Developer Assistant, `/help`, `/review`, Code Reviews и их SQLite-данные не заменены. Примеры, API и lifecycle: [docs/file-agent.md](docs/file-agent.md).

### Архитектура

```text
React File Agent -> FastAPI file_agent/router.py -> service.py (durable jobs/history)
  -> WorkspaceManager: private Git bundle -> isolated detached clone
  -> FileAgent: bounded DeepSeek JSON-action loop -> existing official MCP client
       -> file_agent.mcp_server (FastMCP/stdio) -> Files -> workspace only
       -> exact literal search / related file reads / create / hash-checked patch
       -> existing HybridRetriever (untrusted, SHA-labelled context)
  -> validation.py + configurable rules.py -> Diff -> private patch artifacts
```

`agent.py` динамически выбирает инструменты из настоящего MCP discovery; сценарии не зашиты в orchestration. Поиск классов, импортов, вызовов и строк — точный, без LLM; RAG нужен для смысла и связей. RAG metadata показывает commit и `stale`; изменённый workspace не выдаётся за содержимое общего индекса. Текущие факты проверяются прямым чтением. Цикл ограничен числом действий, временем и накопленным объёмом контекста.

Компоненты планирования изменений и patch application намеренно небольшие: модель выбирает точечную замену, backend проверяет SHA-256 и уникальность старого текста. Это не framework и не выполнение сгенерированного shell.

### MCP File Tools

| Tool | Реальная операция |
|---|---|
| `file_list` | relative scope, glob `pattern`, exclusions, bounded list |
| `file_search` | bounded literal search в нескольких UTF-8 файлах, пути/строки |
| `file_read` | numbered range, SHA-256 исходных байтов, тип/размер, redacted text |
| `file_create` | atomic no-clobber создание нового файла |
| `file_apply_patch` | одна точная уникальная замена `old_text/new_text` с `expected_hash` |
| `file_diff` | полный или per-file redacted unified diff, включая untracked, +/- metadata |
| `file_status` | изменения относительно HEAD, commit, workspace ID, change budget |
| `file_validate` | безопасные Markdown/Java проверки без исполнения проекта |

`rag_search` и `architecture_rules` — дополнительные orchestration tools, не выдаются за MCP. Существующие пять Git MCP tools сохранены отдельно.

### Изоляция, безопасность и ограничения

- Исходный `/repos/aurelSpa:ro` не меняется. Каждая задача получает standalone clone внутри `/data/file-agent/<task>-<id>`; Git worktree не используется, поскольку он записал бы metadata в read-only source.
- Descriptor-based traversal и `O_NOFOLLOW` запрещают symlink, traversal и системные пути; `.git`, `.env*`, keys/credential-файлы и build directories недоступны модели. Hardlinks не используются. Git читает source через config-isolated view: hooks, helpers, replace objects и lazy fetch не исполняются. Shallow snapshot сохраняется; promisor/alternates/gitfile требуют предварительной доверенной standalone-копии.
- Лимиты: 1 MB/файл и write operation, 2 000 файлов, 20 MB scan, 100 изменённых файлов, 5 MB совокупного before+after, tool timeout 30 s. В Docker по умолчанию 32 LLM-итерации, 1200 s/задача, 48 KB текущего и 1.5 MB накопленного контекста, до 8192 output tokens/ответ. Лимит изменений вычисляется заново по HEAD и переживает MCP restart.
- Текст проекта и RAG — недоверенные данные, не инструкции. Redaction маскирует типовые assignment/JSON/XML/YAML credentials, bearer и private keys перед логированием и отправкой модели; это эвристика, а не гарантия обнаружения любого секрета. Не подключайте checkout с секретами, требующими абсолютной неразглашаемости.
- Нет произвольных команд, `push`, автоматических source edits или запуска Maven plugins. Markdown проверяется по заголовкам/локальным ссылкам; Java — по лексическим эвристикам и правилам. Компиляция/тесты Java не исполняются: для них требуется отдельная доверенная sandbox. Успех patch не означает корректность программы.
- REST API рассчитан на одного локального доверенного пользователя; bind только loopback, browser origin проверяется. Не публикуйте его в сеть без authentication/TLS. Не запускайте несколько backend workers с одной очередью SQLite.

### Diff, Apply и воспроизводимость

Результат сохраняется в workspace, история — в новой additive-таблице `file_agent_tasks` с JSON workspace/operations/changes/validation/usage. Существующие таблицы не изменяются. Реальные этапы доступны через polling UI и persistent SSE `/events`; скрытые рассуждения не отображаются.

**Apply Changes** требует подтверждения и проверяет artifact hash, validation, source HEAD, index и исходные hashes/modes. Docker намеренно отказывает: source остаётся read-only. На trusted host запись включается отдельно `AI_HELPER_FILE_AGENT_ALLOW_SOURCE_APPLY=true`; конфликт не перезаписывается. **Discard Changes** удаляет только identity-checked owned workspace; artifacts сохраняются. Детали rollback и редкого partial-apply конфликта описаны в [документе](docs/file-agent.md).

**Re-run Task** заново вызывает ту же модель на том же base snapshot с сохранёнными параметрами; результат не обязан быть побайтово одинаковым. **Replay patch** переносит уже сохранённый patch в новую чистую копию, проверяет integrity и повторяет валидацию без LLM. Приватные bundle + shallow metadata сохраняют базу даже после discard и изменения source refs. Preview diff редактируется и может усекаться; для точного replay используются только private artifacts.

### Запуск и persistence

```bash
docker compose config --quiet
docker compose up -d --build
# Интерфейс localhost:3000 (или FRONTEND_PORT); backend localhost:8000 (BACKEND_PORT).
```

Compose использует существующие `assistant_data:/data` (SQLite, workspaces, private artifacts), `embedding_cache:/model-cache` и source bind `:ro`. Backend root filesystem read-only; временный `/tmp` — ограниченный 256 MB tmpfs, capabilities сняты, `no-new-privileges` включён. Docker socket, home, SSH keys и Git credentials не монтируются. Не выполняйте `down -v`. Дисковые bundles сохраняются после discard: администратор должен контролировать объём и удалять лишь явно выбранные архивы после резервного копирования.

Ключ/модель берутся из существующего DeepSeek client/settings. Нового хранилища ключей нет. Для настройки бюджета используются `FILE_AGENT_MAX_ITERATIONS`, `FILE_AGENT_TIMEOUT_SECONDS`, `FILE_AGENT_MAX_OUTPUT_TOKENS` из `.env.example`; дополнительные backend-параметры перечислены в `backend/app/config.py`. Метрики показывают API tokens (либо помеченную оценку), время, число tools/LLM calls, прочитанных и изменённых файлов.

### Проверки

```bash
cd backend
../.venv-backend/bin/python -m pytest -p no:cacheprovider
../.venv-backend/bin/python -m ruff check --no-cache app tests
cd ../frontend
npm test -- --run
npm run typecheck
npm run build
```

DeepSeek в unit/integration тестах mocked; MCP stdio и файловые операции реальные. Покрыты hash conflicts, traversal/symlink/races, secrets, source apply rollback, shallow snapshots, replay, history/SSE, timeout/error handling и restart recovery. Live демонстрация запускается явно через `scripts/day34_smoke.py --live`; результаты фактической проверки — в `summury.md`.
