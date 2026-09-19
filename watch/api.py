# -*- coding: utf-8 -*-
"""公式結果サイトの裏API。応答は zlib 圧縮バイト列が UTF-8 文字列として届くので latin-1 に戻してから伸長する。"""
import subprocess, zlib, json, time
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
BASE = "https://back.results.asiangames2026.org"

def get(path, retries=2):
    for i in range(retries + 1):
        r = subprocess.run(["curl", "-s", "-m", "30", "-A", UA,
                            "-H", "Origin: https://results.asiangames2026.org",
                            "-H", "Referer: https://results.asiangames2026.org/",
                            BASE + path], capture_output=True)
        b = r.stdout
        if not b:
            time.sleep(1.5); continue
        txt = None
        try:
            txt = zlib.decompress(b.decode("utf-8").encode("latin-1")).decode("utf-8")
        except Exception:
            try:
                txt = zlib.decompress(b).decode("utf-8")
            except Exception:
                txt = b.decode("utf-8", "replace")
        try:
            return json.loads(txt)
        except Exception:
            return txt
    return None

if __name__ == "__main__":
    import sys
    d = get(sys.argv[1])
    s = json.dumps(d, ensure_ascii=False, indent=1) if not isinstance(d, str) else d
    print(s[: int(sys.argv[2]) if len(sys.argv) > 2 else 6000])
