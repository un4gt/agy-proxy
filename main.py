import uvicorn

from app.config import load_config


def main():
    cfg = load_config()
    uvicorn.run("app:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
