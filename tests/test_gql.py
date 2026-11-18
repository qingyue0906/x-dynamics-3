from __future__ import annotations

from xdyn.gql import extract_script_urls, parse_ops_from_js


def test_parse_ops_from_bundle():
    text = 'const x={queryId:"AbCdEfGhIjKlMnOp",operationName:"UserTweets",...};' \
           'params:{id:"QwErTyUiOpAsDfGh",name:"UserMedia",operationKind:"query"}'
    ops = parse_ops_from_js(text)
    assert ops["UserTweets"] == "AbCdEfGhIjKlMnOp"
    assert ops["UserMedia"] == "QwErTyUiOpAsDfGh"


def test_extract_script_urls_modern_and_legacy():
    html = (
        '<script src="https://abs.twimg.com/x-web/entry-client.abc123.js"></script>'
        '<script src="https://abs.twimg.com/responsive-web/client-web/main.abc.js"></script>'
        '<script>self.webpackChunk=[1,{123:"abcdef0",456:"main.chunk"}];</script>'
    )
    urls = extract_script_urls(html)
    assert any("/x-web/" in url for url in urls)
    assert any("main.abc.js" in url for url in urls)
    assert any("456.abcdef0a.js" in url or "123.abcdef0a.js" in url or "main.chunk" in url for url in urls)
