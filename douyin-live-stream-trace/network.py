"""抖音直播流分析 — 共享网络工具"""

import logging
import random
import time
import urllib3
from typing import Optional

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# 进程级连接池，所有函数共享
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """获取共享 requests.Session（懒初始化，带连接池）"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.verify = False
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4,
            pool_maxsize=4,
            max_retries=0,
        )
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
    return _session


def http_get(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int | float = 15,
    retries: int = 3,
    backoff: float = 2.0,
    proxies: Optional[dict] = None,
) -> Optional[requests.Response]:
    """
    带重试的 HTTP GET 请求

    Args:
        url: 请求 URL
        params: 查询参数
        headers: 请求头 (None 则使用默认)
        timeout: 超时秒数
        retries: 最大重试次数
        backoff: 指数退避基础秒数
        proxies: 代理配置 {"http": "...", "https": "..."}

    Returns:
        requests.Response or None
    """
    total_attempts = max(1, retries)

    for attempt in range(1, total_attempts + 1):
        logger.info("HTTP GET attempt %s/%s: %s", attempt, total_attempts, url)

        try:
            response = _get_session().get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
            )

            try:
                response.raise_for_status()
                return response
            except requests.HTTPError as exc:
                status_code = response.status_code

                if 400 <= status_code < 500 and status_code != 429:
                    logger.info("HTTP GET got non-retriable %s for %s", status_code, url)
                    return response

                if status_code == 429:
                    if attempt >= total_attempts:
                        logger.warning("HTTP GET exhausted retries after 429 for %s", url)
                        return None
                    wait_seconds = backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logger.warning(
                        "HTTP GET got 429 for %s, retrying in %.2f seconds",
                        url,
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue

                if 500 <= status_code < 600:
                    if attempt >= total_attempts:
                        logger.warning("HTTP GET exhausted retries after %s for %s", status_code, url)
                        return None
                    wait_seconds = backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logger.warning(
                        "HTTP GET got %s for %s, retrying in %.2f seconds",
                        status_code,
                        url,
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue

                logger.warning("HTTP GET failed with unexpected HTTP error for %s: %s", url, exc)
                return None

        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt >= total_attempts:
                logger.warning("HTTP GET exhausted retries for %s: %s", url, exc)
                return None
            wait_seconds = backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.warning(
                "HTTP GET network error on attempt %s/%s for %s: %s; retrying in %.2f seconds",
                attempt,
                total_attempts,
                url,
                exc,
                wait_seconds,
            )
            time.sleep(wait_seconds)
        except requests.TooManyRedirects as exc:
            logger.warning("HTTP GET too many redirects for %s: %s", url, exc)
            return None
        except requests.RequestException as exc:
            logger.warning("HTTP GET request failed for %s: %s", url, exc)
            return None

    return None


def download_bytes(
    url: str,
    headers: Optional[dict] = None,
    max_bytes: int = 262144,
    timeout: int | float = 30,
    retries: int = 2,
    proxies: Optional[dict] = None,
    chunk_timeout: int | float = 15,
) -> Optional[tuple[bytes, bool]]:
    """
    下载指定大小的数据 (用于 FLV 样本采集)

    Args:
        url: 流地址
        headers: 请求头
        max_bytes: 最大下载字节数 (默认 256KB)
        timeout: 连接超时秒数
        retries: 重试次数
        proxies: 代理配置
        chunk_timeout: 单块读取超时秒数 (防止流式挂死)

    Returns:
        (bytes, is_complete) 或 None — is_complete 为 False 表示数据不完整
    """
    if max_bytes <= 0:
        logger.warning("download_bytes: max_bytes=%s 无效", max_bytes)
        return None

    total_attempts = max(1, retries)

    for attempt in range(1, total_attempts + 1):
        logger.info("Download attempt %s/%s: %s", attempt, total_attempts, url)
        downloaded = bytearray()
        is_complete = True

        try:
            with _get_session().get(
                url,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
                stream=True,
            ) as response:
                response.raise_for_status()

                deadline = time.monotonic() + timeout + chunk_timeout
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue

                    if time.monotonic() > deadline:
                        logger.warning("Download chunk timeout for %s after %s bytes", url, len(downloaded))
                        is_complete = False
                        break

                    remaining = max_bytes - len(downloaded)
                    if remaining <= 0:
                        break

                    downloaded.extend(chunk[:remaining])

                    if len(downloaded) >= max_bytes:
                        logger.info("Download reached max_bytes=%s for %s", max_bytes, url)
                        break

            if downloaded:
                return bytes(downloaded), is_complete
            return None

        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            logger.warning("Download HTTP error for %s: %s", url, status_code or exc)
            if downloaded:
                return bytes(downloaded), False
            return None
        except (requests.ConnectionError, requests.Timeout) as exc:
            if downloaded:
                logger.warning("Download interrupted for %s, returning partial data: %s", url, exc)
                return bytes(downloaded), False
            if attempt >= total_attempts:
                logger.warning("Download exhausted retries for %s: %s", url, exc)
                return None
            wait_seconds = float(attempt) + random.uniform(0, 1)
            logger.warning(
                "Download network error on attempt %s/%s for %s: %s; retrying in %.2f seconds",
                attempt,
                total_attempts,
                url,
                exc,
                wait_seconds,
            )
            time.sleep(wait_seconds)
        except requests.TooManyRedirects as exc:
            logger.warning("Download too many redirects for %s: %s", url, exc)
            if downloaded:
                return bytes(downloaded), False
            return None
        except requests.RequestException as exc:
            logger.warning("Download request failed for %s: %s", url, exc)
            if downloaded:
                return bytes(downloaded), False
            return None

    return None


def download_response_headers(
    url: str,
    headers: Optional[dict] = None,
    timeout: int | float = 15,
    retries: int = 2,
    backoff: float = 2.0,
    proxies: Optional[dict] = None,
) -> Optional[dict]:
    """
    只获取响应头 (HEAD 请求)，用于检查 CDN/Server 信息，带重试

    Returns:
        dict of response headers or None
    """
    total_attempts = max(1, retries)

    for attempt in range(1, total_attempts + 1):
        logger.info("HEAD attempt %s/%s: %s", attempt, total_attempts, url)

        try:
            response = _get_session().head(
                url,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
                allow_redirects=True,
            )
            response.raise_for_status()
            return dict(response.headers)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt >= total_attempts:
                logger.warning("HEAD exhausted retries for %s: %s", url, exc)
                return None
            wait_seconds = backoff * attempt + random.uniform(0, 1)
            logger.warning("HEAD retry %s/%s for %s: %s; %.2fs", attempt, total_attempts, url, exc, wait_seconds)
            time.sleep(wait_seconds)
        except (requests.TooManyRedirects, requests.HTTPError, requests.RequestException) as exc:
            logger.warning("HEAD request failed for %s: %s", url, exc)
            return None

    return None
