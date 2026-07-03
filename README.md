# Smart Port Backend

This backend service provides the FastAPI foundation for the Smart Port multi-agent platform.

## Quick start

1. Copy `.env.example` to `.env`.
2. Start shared local infrastructure from the workspace root:
   - `docker compose -f ../docker-compose.dev.yml up -d`
3. Install dependencies:
   - `pip install -r requirements.txt`
4. Run the app:
   - `uvicorn app.main:app --reload`

## Health check

- `GET /health`
