import uvicorn

from .app import create_app
from .settings import settings

app = create_app()

if __name__ == "__main__":
    uvicorn.run("app.__main__:app", host="0.0.0.0", port=5591, log_level=settings.log_level)
