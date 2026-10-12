# Developer AI Assistant architecture

## Runtime map

```text
React/Vite :3000
  -> /api (nginx proxy)
FastAPI :8000
  -> SQLite WAL (/data/assistant.db)
  -> DeepSeek OpenAI-compatible API
  -> RAG: multilingual MiniLM ONNX + cosine + SQLite FTS5
  -> official MCP client over stdio
       -> read-only Git MCP server
            -> /repos/aurelSpa
```

The target repository is mounted read-only. The application never runs commands
supplied by users and the MCP server exposes only an allowlisted Git interface.

## Component contracts

- `ConversationService`: conversation CRUD, settings, messages, usage and memory.
- `RagIndexer`: incremental scan/chunk/embed/store with immutable source metadata.
- `HybridRetriever.search(query, top_k, threshold, token_budget)`: ranked chunks.
- `GitMcpClient.call(name, arguments)`: official MCP session and structured result.
- `DeepSeekClient`: model discovery, streaming chat and provider usage values.
- `ContextManager`: conservative token estimates and deterministic budget trimming.
- `AgentOrchestrator`: intent routing, RAG/MCP calls, context assembly and metrics.

## API surface

- `GET /api/health`, `GET /api/models`
- `GET|POST /api/conversations`
- `GET|PATCH|DELETE /api/conversations/{id}`
- `GET /api/conversations/{id}/messages`
- `POST /api/conversations/{id}/chat` (SSE)
- `GET /api/conversations/{id}/summary`
- `GET /api/conversations/{id}/usage`
- `GET /api/repository`, `GET /api/mcp/tools`
- `GET /api/rag/status`, `POST /api/rag/reindex`

## Persistence

Conversation deletion cascades to messages. RAG documents and chunks are global
and survive conversation deletion. Summary boundaries are based only on user
messages and are generated after requests 10, 20, 30, and so on.
