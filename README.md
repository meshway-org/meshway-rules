# meshway-rules

Meshway 的规则集发布仓库：把 [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat)（`meta` 分支，domain-list-community／geoip 派生）的全部 geosite 与 geoip 类别转换为 Meshway 规则集格式 `MWRS` v1，组成 ed25519 签名的目录，每天发布到 `dist` 分支。

| 分支 | 内容 |
| --- | --- |
| `main` | 集合清单 [`sources.toml`](sources.toml)、流水线脚本 [`scripts/`](scripts)、workflow [`.github/workflows/publish.yml`](.github/workflows/publish.yml) |
| `dist` | 发布产物；每次发布是一个无父提交并强制推送，同时打 tag `catalog-<sequence>` 并建 GitHub Release 供审计 |

目录地址（raw，不重定向）：

```text
https://raw.githubusercontent.com/meshway-org/meshway-rules/dist/index.json
https://raw.githubusercontent.com/meshway-org/meshway-rules/dist/index.json.sig
https://raw.githubusercontent.com/meshway-org/meshway-rules/dist/artifacts/sha256-<hex>.mwrs
```

## 集合

| 集合 | 上游文件 | 输入格式 | 例 |
| --- | --- | --- | --- |
| `meta-geosite` | `geo/geosite/classical/<name>.list` | `classical` | `meta-geosite/openai`、`meta-geosite/geolocation-!cn`、`meta-geosite/google@cn` |
| `meta-geoip` | `geo/geoip/classical/<name>.list` | `classical` | `meta-geoip/cn`、`meta-geoip/private` |

- 上游每个文件发布为一个集合项，名称即上游文件名（去 `.list`）。
- 使用 classical 文本，因为它是唯一保留 `DOMAIN-KEYWORD`、并列出 `DOMAIN-REGEX` 的文本编码（mihomo domain 文本会静默丢弃这两类）；IP 类别与 `geo/geoip/*.list` 得到相同 artifact。
- 不支持的条目逐条报告并跳过，不使整个集合失败：`DOMAIN-REGEX`（`regex`）、Meshway 规范化不接受的名称（`invalidName`，如含 `_` 或不合法 IDNA 的 punycode）。因此一个集合可能是上游语义的子集；每个集合的 `report` 给出计数，`sets/<collection>/<name>.json` 给出逐条清单（最多 1,000 条）。
- 只含不支持条目的上游文件（如 `meta-geosite/mihoyo@cn` 只有一条 regex）不发布，列在 `status.json` 的 `skipped`。
- 不发布 sing-geosite／sing-geoip：上游只提供编译后的 `.srs`，`meshway-rules` 不解析。

## `dist` 布局

```text
index.json                      签名目录
index.json.sig                  分离签名
artifacts/sha256-<hex>.mwrs     不可变 artifact，文件名即 digest
sets/<collection>/<name>.json   每个集合的溯源与完整转换报告（SetRecord）
history/<sequence>.json         前 6 份目录的原文；它们引用的 artifact 仍保留
status.json                     流水线状态：输入、held、skipped（不签名，供人阅读）
```

### `index.json`

```json
{
  "schema": 1,
  "sequence": 1,
  "generatedAt": "2026-10-12T04:43:01.686599Z",
  "converter": { "version": "0.1.0-alpha.3", "formatMajor": 1, "formatMinor": 0 },
  "upstreams": [
    { "repo": "MetaCubeX/meta-rules-dat", "ref": "meta", "commit": "83c8208bdfae92b1caaa29745f51f233b6f1847e" }
  ],
  "sets": {
    "meta-geosite/openai": {
      "digest": "sha256:44ab0c1a4c37ef326a172e57f7ef86113ebb7d511450eff4606c870823a55fbe",
      "bytes": 592,
      "entries": 22,
      "format": 1,
      "counts": { "exact": 9, "suffix": 13, "subdomain": 0, "keyword": 0, "ipv4": 0, "ipv6": 0 },
      "upstream": {
        "repo": "MetaCubeX/meta-rules-dat", "ref": "meta",
        "commit": "83c8208bdfae92b1caaa29745f51f233b6f1847e",
        "path": "geo/geosite/classical/openai.list"
      },
      "source": { "format": "classical", "sha256": "5aa9a38539026b693135c769f6db10962345ac46860977cb168a52c9d5bfbf6d", "bytes": 812 },
      "report": {
        "converted": { "exact": 9, "suffix": 13, "subdomain": 0, "keyword": 0, "cidr": 0 },
        "redundant": 0,
        "unconverted": { "regex": 1 }
      }
    }
  }
}
```

held 集合另有 `"held": { "since": "<RFC 3339>", "reason": "<原因>" }`（见 [held](#held)）；未 hold 的集合省略该字段。

| 字段 | 说明 |
| --- | --- |
| `sequence` | 每次发布严格加 1；使用者拒绝小于已接受值的目录 |
| `converter` | 构建 artifact 的 `meshway-rules` 版本与 `MWRS` 版本 |
| `upstreams` | 本目录内容实际来自的上游 repo／ref／commit；同一 repo 与 ref 只有一个 commit（held 集合见 [held](#held)） |
| `sets.<key>.digest` | `sha256:` ＋ artifact 全部字节的 SHA-256；`bytes`、`entries`、`counts` 为该 artifact 的事实 |
| `sets.<key>.upstream` | 该集合原文的确切位置 |
| `sets.<key>.source` | 输入格式、原文 SHA-256 与字节数 |
| `sets.<key>.report` | 转换摘要：接受的条目（按 kind）、去冗余数、未转换原因→计数 |
| `sets.<key>.held` | 仅 held 集合：`since`（首次 hold 的发布时间）与 `reason`；同样写在 `sets/<collection>/<name>.json` |

溯源不进入 artifact：上游提交变化而集合内容不变时 digest 不变。

### `index.json.sig`

```json
{ "keyId": "ed25519:<64 hex 公钥>", "signature": "<128 hex>" }
```

签名对象为 ASCII `meshway-rules catalog v1`、一个 `0x00` 字节、再接 `index.json` 的原始字节；以 ed25519 `verify_strict` 验证，`keyId` 必须是下列公钥之一。

| 公钥 | 用途 |
| --- | --- |
| `ed25519:68dfc59cf585e0320129db7f1716b0dbb3d13c462fcefee7151d4b51ae1fc45c` | CI 签名钥：environment `publish` 的 secret，只有 `main` 上的 workflow 可用 |

### held

单个集合不会因上游一次异常而消失或骤缩。以下情况沿用上一版 artifact 与记录，并在签名目录的该集合上写 `held`：

| 情况 | `reason` |
| --- | --- |
| 本次转换失败（原文无法解析、超出上限、无可转换条目） | `conversion failed: <错误>` |
| 规范条目数少于上一版的一半 | `entries dropped from <上一版> to <本次>` |

```json
{ "sets": { "meta-geosite/example": { "digest": "sha256:<hex>", "…": "…", "held": { "since": "2026-10-12T03:17:00Z", "reason": "entries dropped from 4000 to 120" } } } }
```

- `index.json` 的 `sets.<key>.held` 是 hold 的唯一权威记录（签名）：流水线从上一份目录读取 `since`，Meshway Server 在目录浏览与规则集视图中原样显示。`status.json` 的 `held`（另含 `digest`）与 Release 说明只是给人看的副本。
- `since` 是首次 hold 的发布时间，持续 hold 不变；集合恢复正常即解除，`held` 随之省略。
- 上游一致规则：目录的 `upstreams` 列出内容实际来自的 (repo, ref, commit)，同一 repo 与 ref 只能有一个 commit。分支 ref（`meta`）只对应本次转换的 commit；held 集合的内容来自更早的 commit，它的 `upstream.ref` 写成该 commit 本身（git 可直接按 commit 取回），因此 `upstreams` 中多一项 `ref` 等于 `commit` 的条目，多个从同一 commit hold 的集合共用这一项。`ref` 不是分支名即表示 held，与 `held` 字段一致。
- 新出现的 hold 开 issue。确认条目骤减属实后，手动运行 workflow 并在 `accept` 填入集合名（如 `meta-geosite/example`），本次即发布新内容；转换失败的 hold 不能被接受。
- 上游删除的文件从目录中移除（Release 说明列出）。

### 保留

`artifacts/` 保留当前与前 6 份目录（`history/`）引用的全部 artifact，使取到旧目录后再取 artifact 的读取者在实践中不会遇到缺失。更早的 artifact 仍可从对应 `catalog-<sequence>` tag 取得。

## 更新

- 每天 03:17 UTC 运行一次，另可手动运行。上游 commit、`sources.toml` 与转换器版本都未变时不发布。
- 每次运行：按 `ls-remote` 取上游分支当前 commit，按该 commit 拉取原文；每个文件运行 `meshway-rules convert`；应用 held；`meshway-rules index` 组装并签名（sequence 为现有 `dist/index.json` 加 1）；`meshway-rules verify` 验签并核对每个 artifact；强制推送 `dist`（若期间 `dist` 已变则失败）、打 tag、建 Release。
- 转换器是本仓库 Release `cli-v<版本>` 中的 `meshway-rules-x86_64-unknown-linux-musl.tar.gz`，由 Meshway 发版流程推送；workflow 以固定 tag 与 SHA-256 下载，不编译、不需要外部 token。

## 在 Meshway 中引用

Meshway Server 内置两个 provider，指向本仓库 `dist` 的目录并钉住上面的公钥：`geosite` → `meta-geosite`，`geoip` → `meta-geoip`。路由规则中 `geosite:openai`、`geoip:cn` 即引用 `meta-geosite/openai`、`meta-geoip/cn`。Server 启动后及按 provider 的更新间隔（默认 24 小时）自己取回并验签目录，同一目录 URL 与信任钥只取一次，因此未引用任何集合前即可浏览；只接受验签通过、sequence 不回退的目录，失败时保留上一份，并按 digest 取 artifact、核对后使用。字段以 Meshway 文档为准。

## 自己验证

需要 `meshway-rules`（本仓库 Release `cli-v*`，或从 Meshway 源码 `cargo build --release -p meshway-rules-cli`）。

```sh
git clone --depth 1 --branch dist https://github.com/meshway-org/meshway-rules.git dist
meshway-rules verify \
  --public-key ed25519:68dfc59cf585e0320129db7f1716b0dbb3d13c462fcefee7151d4b51ae1fc45c \
  dist/index.json dist
# {"keyId": "...", "sequence": 1, "sets": 2168, "verified": true}

meshway-rules inspect dist/artifacts/sha256-<hex>.mwrs
meshway-rules match dist/artifacts/sha256-<hex>.mwrs api.openai.com
```

`verify` 检查签名，并打开 `index.json` 引用的每个 artifact，核对 digest、字节数、计数与格式版本。钥轮换通过发布钉新公钥的 Meshway Server 版本完成。

## 本地运行流水线

```sh
python3 -m unittest discover -s scripts
mkdir previous    # 或放入现有 dist 的内容
python3 scripts/publish.py build --cli "$(command -v meshway-rules)" --sources sources.toml \
  --previous previous --out out --work work --summary summary.json
meshway-rules index --sign-key <种子文件> out -o out/index.json
meshway-rules verify --public-key <公钥> out/index.json out
```
