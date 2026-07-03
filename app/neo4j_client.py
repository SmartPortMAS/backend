from neo4j import AsyncGraphDatabase

from app.config import get_settings

settings = get_settings()


class Neo4jClient:
    def __init__(self) -> None:
        self.driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    async def close(self) -> None:
        await self.driver.close()


neo4j_client = Neo4jClient()
