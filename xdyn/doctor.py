from __future__ import annotations

import ipaddress
import logging
import socket

from .extract import extract_user
from .gql import USER_FEATURES
from .xclient import XClient

logger = logging.getLogger(__name__)


def _network_hint(config) -> None:
    fake_network = ipaddress.ip_network("198.18.0.0/15")
    found_fake = False
    for host in ("x.com", "pbs.twimg.com"):
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            print(f"{host} 解析：FAIL {exc}")
            continue
        addresses = sorted({str(info[4][0]) for info in infos})
        print(f"{host} 解析：{', '.join(addresses) or '(空)'}")
        for address in addresses:
            try:
                if ipaddress.ip_address(address) in fake_network:
                    found_fake = True
            except ValueError:
                pass
    if found_fake and not config.sync.proxy:
        print(
            "提示：x.com 解析到 198.18.0.0/15（本地代理/VPN 的 fake-IP），"
            "但设置里的代理为空。网络断流时请切换节点，或显式填写代理地址。"
        )


async def run_doctor(config) -> int:
    print("x-dynamics-3 doctor")
    print(f"数据目录：{config.data_dir}")
    print(f"数据库：{config.db_path}")
    print(f"Cookie：{'已配置' if config.has_cookie else '未配置'}")
    _network_hint(config)
    if not config.has_cookie:
        print("失败：请先在 WebUI 设置页填写 auth_token 和 ct0。")
        return 2
    failures = 0
    async with XClient(config) as client:
        try:
            ok, message = await client.auth_check()
            print(f"登录检查：{'OK' if ok else 'WARN'} {message}")
            if not ok:
                failures += 1
        except Exception as exc:
            print(f"登录检查：FAIL {exc}")
            failures += 1
        try:
            await client.transaction.ensure()
            for line in client.transaction.last_diagnostics:
                print(f"  transaction: {line}")
            tid = await client.transaction.generate("GET", "/i/api/graphql/doctor/Doctor")
            print(f"XClientTransaction：OK（{len(tid)} 字符）")
        except Exception as exc:
            print(f"XClientTransaction：FAIL {exc}")
            for line in client.transaction.last_diagnostics:
                print(f"  transaction: {line}")
            failures += 1
        try:
            changed = await client.refresh_ids()
            print(f"queryId 刷新：OK（变化 {len(changed)} 个）")
        except Exception as exc:
            print(f"queryId 刷新：WARN {exc}")
        try:
            payload = await client.graphql(
                "UserByScreenName",
                {"screen_name": "x", "withGrokTranslatedBio": False},
                features=USER_FEATURES,
                field_toggles={"withAuxiliaryUserLabels": True, "withPayments": False},
            )
            user = extract_user(payload)
            if user:
                print(f"GraphQL 请求：OK（@{user.get('screen_name')}）")
            else:
                print("GraphQL 请求：WARN 返回为空")
                failures += 1
        except Exception as exc:
            print(f"GraphQL 请求：FAIL {exc}")
            failures += 1
    print("")
    print("结论：OK" if failures == 0 else f"结论：{failures} 项需要处理")
    return 0 if failures == 0 else 1
