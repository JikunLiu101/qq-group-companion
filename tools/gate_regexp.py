"""根据插件配置里的 gate-secret，算出流水线 group-respond-rules.regexp 要填的正则。

用法（在插件目录下）：
    python tools/gate_regexp.py <gate-secret>
输出是 JSON 字符串（零宽字符以 \\u2063 形式出现），可以直接粘进流水线配置的 regexp 列表。
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from companion.gate import gate_regexp  # noqa: E402

if __name__ == '__main__':
    if len(sys.argv) != 2 or len(sys.argv[1]) < 16:
        sys.exit('用法：python tools/gate_regexp.py <至少 16 位的 gate-secret>')
    print(json.dumps(gate_regexp(sys.argv[1])))
