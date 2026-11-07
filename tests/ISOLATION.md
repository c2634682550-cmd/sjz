# 测试隔离约定（硬规矩）

测试之间**永远不许互相踩**。下面的条款由 `tests/conftest.py` 的 autouse 夹具兜底执行，
不是靠人记得。

## 1. 临时目录：只认 `tmp_path`

- 一切落盘的东西（配置、台账、许可、SQLite、截图）都写到 `tmp_path` / `tmp_path_factory`。
- 禁止读写项目里的真实数据文件：`config.json`、`apps.json`、`cu.json`、
  `data/records.jsonl`、`data/shots/`、`_shots/`、许可库。
- 实现里的常量（`config.CONFIG_PATH`、`records.PATH`、`engine.DATA_DIR`、
  `license` 的状态目录……）一律 `monkeypatch.setattr` 或 setenv 指到 `tmp_path`。

```python
def test_x(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
```

## 2. 端口：只认 0 / 随机可用端口

- 起真 HTTP / socket 服务一律 `("127.0.0.1", 0)`，从 `server_address[1]` 取实际端口。
- 8081 / 8082 这类固定端口只允许作为**被断言的字符串常量**出现在用例里
  （例如 OCR 管家的端口切换逻辑），不许真的去 bind。

## 3. 密钥材料：每次现生成

- 测试密钥从固定种子**当次推导**（`public_key_b64_from_priv(SEED_HEX)`），
  或 `tmp_path` 里现写现用；跑完即弃，不复用仓库里的真实密钥文件。
- 不许把发布私钥 / 许可库拷进测试。

## 4. 环境变量与全局状态

- 用 `monkeypatch.setenv` / `monkeypatch.setattr` 改，测试结束自动还原。
- 进程级全局单例（`license._ENFORCED`、OCR 后端、`config` 缓存……）用完必须复位；
  conftest 已经对 `os.environ` 和 `license._ENFORCED` 做了兜底还原。
- 不要在测试里 `sys.modules[...] = ...`、不要 `importlib.reload` 生产模块。

## 5. 真实文件守护

`_guard_real_files` 会在每个用例前后对上面列出的真实文件算 sha256。
任何用例改动了它们，用例当场失败，报错里写明**哪个用例**改了**哪个文件**
（改前 → 改后哈希）。

## 6. 并行与乱序也必须过

干净的标准是三样一起过：

```powershell
.venv\Scripts\python.exe -m pytest tests -q -n 4
.venv\Scripts\python.exe -m pytest tests -q --random-order-seed=1
.venv\Scripts\python.exe -m pytest tests -q --random-order-seed=2
```

- 用例之间不许共享有序状态（上一条用例留下的文件、缓存、全局开关）。
- 发现单跑绿、全量红，先怀疑「前一条用例留下的线程 / 进程 / 全局桩」，
  再怀疑被测代码。

## 7. 静态兜底（新增测试前自查）

提交前扫一遍：

```powershell
rg -n 'C:\\\\|C:/|Users\\\\|:808[12]|127\.0\.0\.1:8|config\.json"|records\.jsonl' tests
```

命中只允许两类：注释/文档字符串，或「断言固定字符串」的期望值。
真正落盘、真正 bind、真正读写的，一律改回 `tmp_path` / 端口 0。
