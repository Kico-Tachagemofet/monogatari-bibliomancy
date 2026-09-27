# 物语系列书占

从本地物语系列中文 EPUB 中抽取一页，定位到书、分册、话及叙事阶段，再配合两个 skill 做文本细读。抽页由操作系统提供的安全随机完成，问题和页码先写入日志，成功落盘后才输出正文。程序负责素材与位置，skill 负责阅读方法。

**当前为私人 review 测试版。** `books/` 随仓库附带当前收录的 18 本 EPUB，便于复核。书籍是第三方作品，不适用本仓库代码、话卡或 skill 的许可证。本仓库及其历史不应直接改为公开；公开版应从不含书籍的新历史另行发布。

若使用自己的版本，请自备合法取得的中文 EPUB，例如正版中文电子书。本项目不提供外部书本下载或网盘链接。不支持破解 DRM。

## 安装与建库

需要 Python 3.10 或以上。运行工具仅依赖 Python 标准库；测试另需 pytest。以下命令均在仓库根目录执行。

私人 review 版可直接使用 `config.review.json`：

```sh
python tools/monogatari/paginate.py --config config.review.json --build
python tools/monogatari/paginate.py --config config.review.json --check
python tools/monogatari/validate_cards.py --config config.review.json --check
python tools/monogatari/lookup.py --config config.review.json --page 1945
```

首次建库会生成 `.local/library/manifest.json` 和 `.local/library/page_index.json`；它们是本机产物，不提交 Git。当前附带版本应得到 **18 本、36 话、2471 页**。卡片含 **721 个阶段**。第 1945 页可用于核对查询结果，但不是一次书占抽取。

使用自己的 EPUB：

1. 把 `config.example.json` 复制为 `config.local.json`。
2. 修改 `epub_dir`，指向你的 EPUB 文件夹。相对路径以配置文件所在目录为基准，也支持绝对路径。
3. 保留或设置 `state_dir`、`log_path`。换译本时使用新的 `state_dir` 和日志，保留旧库以便恢复旧抽取。
4. 执行 `python tools/monogatari/paginate.py --build`，再执行 `--check`。省略 `--config` 时读取根目录的 `config.local.json`。

配置字段：

| 字段 | 意义 |
|---|---|
| `epub_dir` | 输入 EPUB 目录，只读，不递归扫描 |
| `state_dir` | 本地 manifest 和页码表所在目录 |
| `log_path` | 不含正文的抽取日志路径 |
| `target_chars` | 目标页长，默认 1100 个非空白字符，标点计入 |
| `merge_tail_below` | 每话末页低于该值则并入前页，默认 400 |
| `fallback_threshold` | 超长单元的回退阈值，默认 1600 |
| `include_books`（可选） | 选定的书/分册键；默认要求全部 18 本齐全。无分册写 `-` |
| `epub_files`（可选） | 显式参与建库的文件名列表，用于混合目录；只筛范围，不能代替元数据认书 |
| `overrides`（可选） | 本地覆盖 JSON 的路径 |

例如只建《伤物语》，可加 `"include_books": ["伤物语/-"]`；目录中有其他系列或未收录书时，再用 `epub_files` 明确列出要读取的文件。每个书/分册只能有一个版本。未知书、目录冲突或分册歧义会报错，不根据文件名猜测。

**登记制：** 首次必须显式 `--build`。建库把每个文件的 SHA256 记入本地 manifest。此后的 `draw`、`show`、`--check` 只验证登记书，忽略未登记文件。任何登记书缺失、被改动、改名，或本地索引/规则漂移，都拒绝读取正文或消费随机。`--build` 也不能静默接受已登记源的变动；确需换版本，另建库。

## 认书、分话与本地覆盖

认书使用 OPF 标题与 NCX/EPUB3 nav 中的实际话名，归一化常见全半角、标点及部分简繁差异。分册由明确标题或匹配的话卡确认。目录话名与卡片匹配后，按 spine/anchor 坐标定边界；话的编号对应卡片的 `书名/分册/本册话序号`，不是跨册总话号。文件名与哈希不用于猜书。哈希只在本地登记后用于拒绝源变动。

特殊规则按书/分册登记：猫白维持单话且话名留空；佰物语保留原编号片段，缺号不补；部分分册明确排除首话前的材料；终中识别重复卷/章标题。规则不写死 EPUB 内部文件名。未知版式仍需人工核对。

如果报错提示补本地覆盖，把 `overrides.example.json` 复制为 `overrides.local.json`，在配置里增加 `"overrides": "./overrides.local.json"`。覆盖格式示例（占位文件及坐标须换成实际值）：

```json
{
  "identities": {
    "your-edition.epub": ["伤物语", ""]
  },
  "books": {
    "伤物语/-": {
      "arc_aliases": {"1": ["你这版的实际目录话名"]}
    }
  }
}
```

`identities` 是使用者对已检查文件的明确认书声明；自动识别不会回退到文件名。`books` 中的规则按书/分册生效。若别名仍不足，可使用：

```json
{
  "identities": {},
  "books": {
    "伤物语/-": {
      "boundaries": [{"arc_no": 1, "spine": "Text/chapter.xhtml", "paragraph": 0}],
      "item_exclusions": {"Text/front.xhtml": "已检查的前置材料"},
      "paragraph_exclusions": [{"spine": "Text/chapter.xhtml", "paragraph": 1, "reason": "重复标题"}]
    }
  }
}
```

`spine` 是 ZIP 中完整路径；段号从 0 开始，包括随后会剔除的标题段；边界位于段首。多话时须按本册话序完整列出全部边界。不支持把话边界猜到段中。可用标准库交互查看解析后的标题与段号（会显示本地文本，请勿把整段输出粘到公开问题中）：

```python
from pathlib import Path
from tools.monogatari.epub_text import read_epub
epub = read_epub(Path("books/your-edition.epub"))
print(epub.titles, epub.toc)
for item in epub.spine:
    print(item.path, list(enumerate(item.paragraphs)))
```

修改覆盖后重新显式 `--build`、`--check`。如果旧库已有抽取记录，保留旧配置及产物，新建 `state_dir`，确保旧日志中的索引校验值仍可恢复。

## 抽页、恢复、前情

实际书占先逐字确认问题，再执行一次：

```sh
python tools/monogatari/draw.py --config config.review.json draw --question "已经确认的问题原文"
```

一次调用就是一次不可重抽的抽取。使用均匀安全随机选页；先写入问题、UTC 时间、页码和页码表 SHA256 并同步磁盘，再读取并输出正文。日志没有原文。日志损坏或锁被占用时，随机开始前停止。随机已消费而写入/输出失败时，错误会说明已选页和不确定状态；不得再次运行 `draw`，应核对日志并用已选页恢复：

```sh
python tools/monogatari/draw.py --config config.review.json show --page N
python tools/monogatari/lookup.py --config config.review.json --page N
```

`N` 换成日志中的整数。`show` 不抽取、不写日志。`lookup` 不抽取、不写日志、不读取 EPUB 正文，只读本地索引与卡片，并核对 manifest 与索引的绑定。它不是源文件哈希复验的替代品；使用正文前应通过 `show` 或 `--check`。

查询显示出处、话卡、所在阶段、本页在阶段内的位置、上一段摘要，以及“前情可查”的页码范围。只可用 `show` 读取这个范围内本页之前的页。前情只作背景，不替代抽到的页；**本页之后一页都不看、不引用**。本工具不输出下一页语境。阶段人物栏可能概括本页之后才发生的事，须以本页原文和已读前情为准。

追问始终沿用同一页。不因不好解读而重抽，也不用 `show` 挑页。除恢复外，`show` 仅用于同段前情和重看已抽页。程序允许按页查询以支持 review，具体阅读边界由使用者与 skill 遵守。

三个读取入口支持 `--json`。控制台编码有问题时设置 `PYTHONIOENCODING=utf-8`。

## 两个 skill 的安装

- `skills/monogatari-bibliomancy/`：素材层，负责确认问题、抽页、查询、前情与交接。
- `skills/bibliomancy-reading/`：通用细读法，负责场景、意象、互动、人物时期、映射、证据分层及反馈。

在此仓库作为工作项目时，把两个完整目录复制到相应项目目录：Claude Code 使用 `.claude/skills/`；Codex 使用 `.agents/skills/`。最终路径例如 `.agents/skills/monogatari-bibliomancy/SKILL.md`。复制目录即可，无须改解读规则。

skill 中的命令相对仓库根目录执行，默认使用 `config.local.json`。review 时把 `config.review.json` 复制为 `config.local.json` 即可。若安装到其他项目，请明确让代理在本仓库执行这些命令，或将命令里的脚本路径改为此仓库实际位置；不要根据客户端名称推测路径。

素材层必须实际调用通用阅读 skill，不能把话卡当答案。引用只取定位画面所需的短句；归档先预览、确认后只新建，不覆盖。两个 skill 的解读规则保持原样，测试版仅调整路径和书籍分发说明。

## 收录范围与限制

当前 18 本：伤物语、伪物语（上/下）、佰物语、倾物语、凭物语、化物语（上/下）、历物语、囮物语、恋物语、猫物语（黑/白）、终物语（上/中/下）、花物语、鬼物语。其他 14 本后续材料不在本版数据及测试范围内。

- 全局页序使用固定的书/分册目录顺序，与附带版本的基准顺序一致，不表示出版或剧情顺序；重命名文件不会改变页序。
- 每话独立按句末分页，优先接近目标字数；短尾页合并，长句回退到段末或逗号安全边界。段内空白保留，字数不计空白。
- 阶段以本话累计非空白字数的进度比例定位，精度为 0.001。优先恢复可唯一对应的本地页首；不同译本不能对应时取该比例所在页，边界通常可能相差一两页，这不是所有译本的保证。出现阶段塌缩或歧义则拒绝查询，需本地检查。
- 怪异、主角、主旨三栏仍作为草稿提供，欢迎提出具体纠正。保留既有 `status` 机器字段，不把少量历史批准标记解释成全量完成。
- 人物栏和阶段摘要是背景，不是本页证据；也不能借它们提前透露后续。
- 支持无 DRM、可读 UTF-8 XHTML、单 OPF、NCX 或 EPUB3 toc nav 的 EPUB。非标准排版、未涵盖的简繁或译名差异需要覆盖配置；合订本、多版本混放、段中话边界不会自动猜测。

## 测试与验收

```sh
python -m pip install "pytest>=8,<10"
python -m pytest -q
```

测试在临时目录自造 EPUB，正文为虚构短句，不使用 `books/`。复制 `tools/`、`data/`、`tests/` 和 `pyproject.toml` 到一个没有真实 EPUB 的目录，测试仍应通过。覆盖元数据识别、EPUB2/3、锚点分话、内部路径变化、文件名变化、歧义拒绝、本地覆盖、分页、登记制、日志落盘与失败恢复、查询前情及卡片结构。

卡片校验包含连续 12 字防摘录，去空白和标点并跨页检查，保留既有专名豁免；诊断只输出字段、位置和哈希。没有本地书库时仍检查结构，并明确打印 `SKIP`；部分缺书只跳过缺失来源，已存在却哈希不符的书会报错。`CARDS OK` 同时有 `SKIP` 时不代表防摘录全量通过。

review 时打开本 README，按“安装与建库”执行：应看到 2471 页、36 话、721 段；第 1945 页查询应包含阶段内页位和前情范围。运行 `show` 不应新增日志，不应输出下一页语境。测试和 `--check` 不会代替你抽取一次真实问题。

## 许可证

版权署名：**Kico-Tachagemofet**。

- `tools/`、`tests/` 中的代码：MIT，见 [LICENSE](LICENSE)。
- `data/` 的话卡与 `skills/` 的文字：CC BY-NC-SA 4.0，见 [LICENSE-CONTENT](LICENSE-CONTENT) 和 [许可全文](https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.en)。适用范围仅限维护者有权许可的贡献。
- `books/` 中的第三方 EPUB、作品原文、原作者和译者权利不在上述许可范围内；本仓库不授予这些材料的再分发许可。
