"""Optional SearXNG JSON search tool. No model/API credentials are sent to this endpoint."""
import html
import json
import os
import re
from urllib.parse import urlsplit

import requests
from .credentials import validate
from .ops import OpError


def parse_results(data):
    rows=[]
    for item in data.get('results',[]):
        if not isinstance(item,dict):continue
        url=item.get('url','')
        if not isinstance(url,str):continue
        try:parsed=urlsplit(url)
        except ValueError:continue
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:continue
        def text(value):return html.unescape(re.sub(r'<[^>]*>',' ',str(value or ''))).strip()[:1200]
        rows.append(dict(title=text(item.get('title')),url=url[:2048],snippet=text(item.get('content'))))
        if len(rows)>=5:break
    return rows


def search(query):
    endpoint=os.environ.get('LECHONK_SEARCH_URL','').strip()
    if not endpoint:
        raise OpError('External web search is not configured. This preview model has no Studio builtin connectors. Use wiki and game-file tools, or ask the player to configure LECHONK_SEARCH_URL to a SearXNG JSON /search endpoint.')
    if not query.strip() or len(query)>600:raise OpError('Search query must be 1..600 characters')
    try:validate({'provider':'custom','base_url':endpoint,'model':'search'})
    except ValueError:raise OpError('Search URL must be HTTPS or localhost HTTP, without credentials/query/fragment') from None
    try:
        with requests.get(endpoint,params={'q':query,'format':'json'},timeout=(10,25),
                          allow_redirects=False,stream=True) as response:
            if response.status_code!=200:raise OpError('Search endpoint returned HTTP %d; use the local wiki if unavailable'%response.status_code)
            body=bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body)>1024*1024:raise OpError('Search response exceeded 1 MiB')
            rows=parse_results(json.loads(body))
    except (requests.RequestException,ValueError,AttributeError):
        raise OpError('Search endpoint unavailable or not SearXNG JSON; local wiki remains available') from None
    if not rows:raise OpError('Search returned no usable results; try another query or the local wiki')
    return rows
