# -*- coding: utf-8 -*-
import httpx
import config

from trippostcollect.runtime.http import make_async_client as _make_async_client


def make_async_client(**kwargs) -> httpx.AsyncClient:
    return _make_async_client(
        disable_ssl_verify=getattr(config, "DISABLE_SSL_VERIFY", False), **kwargs,
    )
