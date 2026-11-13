# framescope

给一份贴过来的 Python 报错回溯，把每一帧对应到本地源码：打印出错行前后的真实代码、把解释器当时印出的那一行拿来核对，并且说清楚任何一帧为什么显示不出来。

只读，标准库，单文件，零配置。

---

## 要解决的问题

报错回溯通常来自另一台机器、另一次构建，或者几周前的一次运行。它里面带着的是**当时**的绝对路径和行号：

```
  File "/home/runner/work/proj/src/app.py", line 9, in load
    with open(path, encoding="utf-8") as handle:
```

要看懂它，得手工做三件事：把那个路径映射到本机代码库、翻到第 9 行、再判断本机的第 9 行是不是当时那一行。第三件最容易被跳过——代码改过之后，行号还在，内容已经不是一回事了，照着旧行号读代码会得出错误结论。

`framescope` 一次做完这三件事，并把每一步的依据都印出来。

**输入**：一段回溯文本（文件或标准输入），加一个本地源码根目录。
**输出**：每一帧的源码上下文、核对结论、以及无法显示的帧的具体原因。
**边界**：不执行代码、不导入被查看的模块、不联网、不写任何文件；只按回溯里的 `File "…", line N` 行工作；日志里的路径永远不会拼到根目录上，因此不可能读到根目录之外的文件。

---

## 用法

```bash
# 从文件读，根目录就是当前项目
python framescope.py traceback.txt

# 从管道读，指定源码根目录
cat ci.log | python framescope.py --root ./src -C 2

# 把每一条候选路径和核对过程都打出来
python framescope.py traceback.txt --root . --explain

# 给脚本用
python framescope.py traceback.txt --root . --json > report.json
```

参数：

| 参数 | 默认 | 作用 |
|---|---|---|
| `LOG` | `-`（标准输入） | 回溯文件；`-` 表示读标准输入 |
| `--root DIR` | `.` | 本地源码根目录 |
| `-C, --context N` | `2` | 出错行前后各显示几行，允许 0–20 |
| `--explain` | 关 | 打印候选后缀阶梯、日志行与本地行的逐项核对 |
| `--json` | 关 | 输出单个 JSON 文档 |
| `--version` | — | 打印版本号 |

退出码：`0` 所有帧都显示出来且核对通过；`1` 出现 `stale` / `missing` / `ambiguous` / `bad-line`；`2` 输入里没有帧、文件读不了或根目录不存在。可以直接拿去当流程闸门。

---

## 真实输出

`examples/demo/traceback.txt` 是真实运行 `examples/demo/src/app.py` 捕获的回溯。

```bash
$ python framescope.py examples/demo/traceback.txt --root examples/demo -C 1
```

```
framescope  root=C:\Users\Administrator\repos\framescope\examples\demo  context=1

  [1] C:\Users\Administrator\repos\framescope\examples\demo\src\app.py:19 in <module>
      -> src/app.py                   exact path under root
      18 | if __name__ == "__main__":
  >   19 |     main()
         |     ~~~~^^
      ok        local line 19 matches the line printed in the log

  [2] C:\Users\Administrator\repos\framescope\examples\demo\src\app.py:15 in main
      -> src/app.py                   exact path under root
      14 | def main():
  >   15 |     return load("stats.json")
      16 | 
      ok        local line 15 matches the line printed in the log

  [3] C:\Users\Administrator\repos\framescope\examples\demo\src\app.py:9 in load
      -> src/app.py                   exact path under root
       8 | def load(path):
  >    9 |     with open(path, encoding="utf-8") as handle:
         |          ~~~~^^^^^^^^^^^^^^^^^^^^^^^^
      10 |         data = json.load(handle)
      ok        local line 9 matches the line printed in the log

summary  1 stack(s) · 3 frame(s) · 3 ok · 0 no-echo · 0 stale · 0 skip · 0 missing · 0 ambiguous · 0 bad-line
```

`>` 标出出错行，下一行的 `~^` 是解释器给出的列位置，按本地行的缩进重新对齐。

### 路径来自另一台机器、并且源码已经改过

`examples/ci/traceback.txt` 是在另一个目录里真实运行脚本捕获的回溯；`examples/ci/app.py` 是本机现在的代码，第 9 行已经换成了 `if __name__ == "__main__":`。

```bash
$ python framescope.py examples/ci/traceback.txt --root examples/ci -C 1 --explain
```

```
framescope  root=C:\Users\Administrator\repos\framescope\examples\ci  context=1

  [1] C:\Users\Administrator\framescope_ci_probe\app.py:9 in <module>
      -> app.py                       suffix, 3 leading part(s) dropped
       8 | 
  >    9 | if __name__ == "__main__":
      10 |     build({"total": 10, "count": 0})
      stale     local line 9 differs from the log; the printed line appears at line 10
      echo    build({"total": 10, "count": 0})
      caret   ~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^
      local   if __name__ == "__main__":
      also at lines 10
      candidates (longest suffix first):
        Users/Administrator/framescope_ci_probe/app.py 0 hit(s)
        Administrator/framescope_ci_probe/app.py       0 hit(s)
        framescope_ci_probe/app.py                     0 hit(s)
        app.py                                         1 hit(s)

  [2] C:\Users\Administrator\framescope_ci_probe\app.py:6 in build
      -> app.py                       suffix, 3 leading part(s) dropped
      ...
```

这里做了两件关键的事：路径按最长后缀阶梯落到 `app.py`；日志当时印的是 `build({...})`，而本地第 9 行已经不是它了，所以判为 `stale`，并且在同一文件里找到那句话现在位于第 10 行。这一帧的退出码是 `1`。

注意日志里的 `~^` 属于**当时**那一行，源码一旦对不上，就不会画在本地代码下面，而是在 `--explain` 里单独列出，避免误导。

### 显示不出来的帧

```bash
$ printf 'Traceback (most recent call last):\n  File "<frozen importlib._bootstrap>", line 1387, in _find_and_load\n  File "/build/proj/gone.py", line 4, in run\n    broken()\nRuntimeError: x\n' | python framescope.py --root examples/demo
```

```
framescope  root=C:\Users\Administrator\repos\framescope\examples\demo  context=2

  [1] <frozen importlib._bootstrap>:1387 in _find_and_load
      skip      virtual filename, no file on disk

  [2] /build/proj/gone.py:4 in run
      missing   no file under root ends with any suffix of the log path

summary  1 stack(s) · 2 frame(s) · 0 ok · 0 no-echo · 0 stale · 1 skip · 1 missing · 0 ambiguous · 0 bad-line
```

尖括号文件名本来就没有磁盘文件，标为 `skip`，不影响退出码；真正找不到的帧标为 `missing`，会把试过哪些后缀都列出来。

---

## 七种结论

| 状态 | 含义 |
|---|---|
| `ok` | 找到了本地文件，且本地行与日志印出的那一行一致 |
| `no-echo` | 找到了文件，但日志没有印出源码行（例如旧格式或被截断的日志），无法核对 |
| `stale` | 找到了文件，但本地这一行已经不是日志里的那一行；会给出该行现在的位置 |
| `missing` | 根目录下没有任何文件的路径以日志路径的某个后缀结尾 |
| `ambiguous` | 最短后缀同时命中多个文件，不猜，把候选全部列出 |
| `bad-line` | 文件找到了，但行号超出了文件行数 |
| `skip` | 尖括号虚拟文件名，磁盘上没有对应文件 |

`--json` 会输出同样的结构，附带每一帧的候选阶梯、上下文行号与文本、以及汇总计数：

```bash
$ python framescope.py examples/demo/traceback.txt --root examples/demo --json
```

下面贴的是实际输出里**第一帧**的内容（为节省篇幅，另外两帧与 `summary` 省略）：

```json
{
  "tool": "framescope",
  "version": "1.0.0",
  "root": "C:\\Users\\Administrator\\repos\\framescope\\examples\\demo",
  "context": 2,
  "index": { "files": 2, "truncated": false },
  "stacks": [
    {
      "index": 1,
      "notes": [],
      "frames": [
        {
          "file": "C:\\Users\\Administrator\\repos\\framescope\\examples\\demo\\src\\app.py",
          "line": 19,
          "function": "<module>",
          "status": "ok",
          "resolved": "src/app.py",
          "reason": "local line 19 matches the line printed in the log",
          "echo": "main()",
          "local": "    main()",
          "caret": "    ~~~~^^",
          "relocations": [],
          "candidates": [
            { "path": "C:\\Users\\Administrator\\repos\\framescope\\examples\\demo\\src\\app.py", "hits": 1 }
          ],
          "hits": ["src/app.py"],
          "context": [
            { "n": 17, "text": "", "target": false },
            { "n": 18, "text": "if __name__ == \"__main__\":", "target": false },
            { "n": 19, "text": "    main()", "target": true }
          ]
        }
      ]
    }
  ]
}
```

完整文档请直接运行命令查看。

---

## 设计取舍

**为什么按后缀阶梯找文件，而不是猜一个前缀。** 日志里的绝对路径和本机目录结构没有任何可靠对应关系，硬编码「砍掉前 N 段」会在项目层级变化时失效。做法是把根目录下每个真实文件的**所有路径后缀**建成索引，然后把日志路径从最长后缀开始逐级缩短去查，第一个唯一命中就是答案。这样 `/work/cpython/Lib/glob.py` 既能落到 `glob.py`，也能让 `/work/cpython/Lib/zipfile/_path/glob.py` 落到那个更深的文件，两者不会互相抢占。

**为什么还要把根目录自己的名字加进索引。** 标准库里 `glob.py` 同时存在于根层和 `zipfile/_path/` 下，只靠 `glob.py` 这个后缀会撞车。而日志路径里通常带着检出目录名（`.../Lib/glob.py`），所以额外登记 `Lib/glob.py → glob.py`，让更长的后缀先命中。实测这一步把 500 帧语料里的 5 处 `ambiguous` 全部消掉了。

**为什么不把日志路径直接拼到根目录上。** 日志是外部文本，可能含 `..` 或绝对路径。索引里的键全部来自根目录内真实存在的文件，查到的是真实相对路径，再拼回根目录，因此天然不会越界。查不到就是 `missing`，不会去碰别处的文件。

**为什么拿日志印出的源码行做核对。** 行号是最不可靠的信息：代码一改，行号还在、含义全变。解释器在 `File …` 行下面印出的那句源码，是当时那一行的直接证据。本地行与它一致才判 `ok`；不一致就判 `stale`，并在整个文件里找这句话现在在哪一行。宁可明确说「对不上」，也不假装第 9 行还是原来那行。

**为什么 `ambiguous` 不选一个。** 猜错文件比不显示更危险——它会让人对着一份不相干的代码推断 bug。命中多个就全部列出，让用户用更精确的 `--root` 收窄范围。

**为什么虚拟文件名算 `skip` 不算失败。** `<frozen ...>`、`<string>` 这类帧在正常回溯里必然出现，磁盘上不存在是预期结果，不该让退出码变红。

**为什么默认只读 2 行上下文。** 一帧的信息量在出错行本身，前后两行足够看出函数边界和缩进；需要更多用 `-C`，不需要就不要刷屏。

**其他细节**：源码按 UTF-8（带 BOM 也能读）解码，坏字节替换为占位符而不是崩掉；日志与源码的 CRLF 都按行切分处理；忽略 `__pycache__`、`.git`、`node_modules` 等目录；文件数超过 50000 会明确提示索引被截断；Windows 控制台强制 UTF-8 输出。

---

## 实测

真实标准库目录（1785 个文件）+ 一份 500 帧、60.4 KB 的回溯日志，路径统一加了构建机前缀、源码行取自真实文件：

- 两轮取较快：建索引 0.1120 s，500 帧解析 + 定位 + 核对 0.2221 s，进程内合计 **0.3341 s**
- 命令行端到端（含解释器启动与 848 KB JSON 输出）两轮取较快 **0.7512 s**
- 结果：**423 帧 `ok`、77 帧 `no-echo`（这些行本身是空行，日志无内容可核）、0 帧 `ambiguous`、0 帧 `missing`**

测试：

```bash
$ python -m unittest discover -s tests -v
Ran 106 tests in 35.035s
OK
```

---

## 文件

```
framescope.py              实现，单文件，仅标准库
tests/test_framescope.py   106 个测试
examples/demo/             本机捕获的回溯 + 对应源码
examples/ci/               另一目录捕获的回溯 + 已改动的本地源码
LICENSE                    MIT
```

## 许可证

MIT
