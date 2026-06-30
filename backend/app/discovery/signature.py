"""去重签名：按 Recipe 关键特征算 hash，用于 crawl_method_domains 查重。"""

import hashlib
from urllib.parse import urlparse

from app.discovery.dsl import DslRecipe, FetchAction, GotoAction, LoopAction, ExtractAction


def compute_signature(recipe: DslRecipe) -> str:
    """签名 = hash(entry_url + fetch_host + extract_from + has_loop)。

    粗筛：命中后用产出链接比对兜底。同形 Recipe（如 {no}/{id} 占位符不同）视为同。
    """
    # 第一个 fetch/goto 的 URL host
    host = ""
    for a in recipe.actions:
        if isinstance(a, (FetchAction, GotoAction)):
            host = urlparse(a.url).netloc
            break
    # extract.from + 是否含 loop（决定能否翻页）
    extract_from = ""
    has_loop = False
    for a in recipe.actions:
        if isinstance(a, ExtractAction):
            extract_from = a.from_
        if isinstance(a, LoopAction):
            has_loop = True
    key = f"{recipe.entry_url}|{host}|{extract_from}|{has_loop}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]
