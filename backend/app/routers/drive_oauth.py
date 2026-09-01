"""Compatibility re-export. Carousel Studio OAuth lives in ``carousel_oauth``."""
from app.routers.carousel_oauth import resolve_oauth_return_url, router

__all__ = ["router", "resolve_oauth_return_url"]
