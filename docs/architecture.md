# 弹幕迁移工作台 · 架构评审稿

工作台支持 Windows 免配对码本机会话、项目、B 站下载、字幕输入、LLM 分析、人工审核及 B 站投递。API 保留合成下载与分析。投递测试通过注入模拟平台适配器执行。迁移项目保存一对视频分 P、多个下载快照、字幕版本和分析运行。下载、分析、投递各自创建任务，工作期间可以关闭浏览器。

## 边界与目录

```text
backend/
  pyproject.toml
  src/danmaku/
    main.py             应用工厂，组装依赖
    api/                HTTP、会话依赖、SSE 序列化
    domain/             Pydantic 值对象、枚举、错误码
    application/        用例及 Protocol 端口
      download/ subtitles/ classification/ review/ sending/ tasks/
    adapters/           B站、解析器、模型服务商、Mock
    persistence/        SQLAlchemy 映射、仓储、事务
  tests/
frontend/
  src/
    main.tsx            浏览器入口
    api/                类型、HTTP 客户端，后续封装 EventSource
    features/           项目、设置、审核、任务各自的视图和交互
  tests/
docs/                   架构契约、分期验收
```

浏览器 → 同源 REST → 用例 → 端口 → 适配器。HTTP 层负责认证、校验和状态码映射。应用服务负责事务边界、审核策略与调度。ORM 对象只在持久化模块使用。Pydantic DTO 作为模块边界，前端按 FastAPI OpenAPI 校对类型。单进程 FastAPI 启动一个受 lifespan 管理的 worker；SQLite 是任务队列与状态的事实来源。网络等待异步执行，同步解析与数据库事务交给受限线程，每次事务独立 Session。外部调用期间释放数据库写锁。

## 模块契约

|模块 / Python 端口|输入|输出|失败及处理|
|---|---|---|---|
|下载 DownloadSource.parts / fetch|BV 号，或固定 VideoPart、从 0 起的分段索引和计划段数|分 P 列表，或仅含白名单字段的 SourceBatch|分 P 不明要求选择。权限、限流、接口不可用分别报告。瞬态错误重试同一分段，部分下载保持草稿|
|字幕 SubtitleParser / Subtitles.upload|ASS/SRT/TXT 文本（TXT 按内容识别 ASS 或 SRT）、格式、可用时的片长上界，或至多 2 MiB 的文件字节及编码|识别后的格式、按起点排序的 Cue 列表、跳过项及警告、文件指纹|解码失败可指定编码后重试，空文件单独报告，非法时间段报告行号，相同输入复用版本|
|分类 Analyze / ModelProvider.generate|已完成快照、至少一种语言的字幕版本、模型配置。一次发送所选完整字幕与弹幕，使用匿名 ID 和相对时间|分析 run、逐 ID 的类别、可选理由及字幕引用|严格校验后整次写入。限流及瞬态网络故障整次退避，超限和格式错误直接结束，凭据问题暂停|
|审核 ReviewService.list / apply|项目、已结束的分析运行、筛选及游标，或预期审核修订、ID 集、勾选值、操作者|审核分页及整运行已选数，或提交修订、变更数和整运行已选数|资源不可见返回 404。版本冲突整批回滚，重新读取后由用户核对再提交。数据库繁忙可重试|
|发送 Sender.send|固定目标 CID、投递项、账号引用、显示属性|确认回执、明确拒绝或结果未知|不支持效果给出明细；不可确认结果转人工核对|
|调度 TaskService / Worker|命令、版本、持久化游标、时钟|任务快照、进度事件、下次执行时间|非法转换返回冲突；重启按下述规则恢复|
|持久化 UnitOfWork / Repository|带项目范围的查询、变更集|DTO、游标或已提交版本|唯一约束去重、版本比较并交换；锁忙短退避，耗尽返回可重试错误|
|配置 SettingsService|主体、服务配置、只写密钥|配置摘要、密钥引用、是否已配置|缺密钥暂停依赖任务，错误不携带请求体|
|凭据 CredentialStore|写入 `SecretStr`，按不透明引用读取或删除|写入返回新引用，读取缺失时返回空，重复删除成功|临时存储错误返回可重试的脱敏异常。引用提交失败时调用方清理新凭据|

按需安装 bilibili-api-python 17.4.2，并使用 HTTPX。分 P 查询使用匿名请求。获取当前弹幕分段前，后端按任务创建者读取已保存的 B 站登录凭据。未配置账号时使用匿名请求。已绑定账号但凭据丢失时，任务报告 `bilibili_credential_missing`，保留已提交分段。SDK 的历史接口按日期查询并要求 SESSDATA，权限与可取日期范围待真实网络验证。上游已停止维护，本项目继续使用固定版本。[发布版本](https://pypi.org/project/bilibili-api-python/17.4.2/) · [上游仓库](https://github.com/Nemo2011/bilibili-api)

pysubs2 的事件起止为毫秒；可用 plaintext 去除 ASS 覆盖标签，is_text 过滤注释和绘图。保留同时间、多行及重叠对白，展示时可按样式筛选。[pysubs2 API](https://pysubs2.readthedocs.io/en/latest/api-reference.html)

## 数据与可追溯性

持久 ID 对外使用字符串，视频时间统一整数毫秒，系统时间使用 UTC RFC 3339。以下为逻辑实体。分析输入在创建事务内按项目核对，分类记录以复合外键绑定运行和源条目。每次任务保留创建人和输入版本。

|实体|关键字段与约束|
|---|---|
|Project / VideoPart|owner_id、source/target 的 bvid、page（从 1 起）、cid、duration_ms；快照固定 CID|
|SourceSnapshot / SourceItem|抓取参数、覆盖范围、状态、计数；快照内存源条目 ID、原始文本、时间、RGB、字号、平台模式。允许相同时间和文字的不同弹幕|
|NormalizedItem|source_item_id、text、time_ms ≥ 0、color_rgb 0..16777215、font_size、mode、normalizer_version、稳定来源键。分段重试优先使用平台弹幕 ID，缺失时使用同一快照内固定分段与序号；不确定的跨快照匹配需核对|
|SubtitleVersion / Cue|原始文件字节、SHA-256、编码、解析器版本；cue_id、start_ms、end_ms、plaintext、style、原顺序|
|AnalysisRun / Classification|源快照、所选日语和中文字幕版本（至少一项）、模型及服务商配置版本、提示模板版本、上下文策略、状态；唯一 (run_id,item_id)，category、reason、cue_ids 和诊断码|
|ReviewDecision|run_id、item_id、selected、项目审核修订、操作者、修改时间；追加记录，每项最新修订生效|
|ReviewText|按 run_id、item_id 保存改写文本，沿用项目审核修订。审核搜索、投递预览和新投递批次使用改写文本，恢复原文时删除改写记录|
|Task / TaskEvent|kind、project_id、creation_order、state、version、输入引用、checkpoint、done、total（未知为 null）、next_attempt_at、error；事件按 task_id、seq 唯一|
|SendBatch / Delivery / Attempt|review_revision、run_id、目标 CID、mock/live、冻结的条目和显示参数；投递状态、尝试序号、脱敏回执、错误码、发送时间|
|Settings / SecretRef|owner_id、provider、model、endpoint、配置版本、secret_ref；数据库只存引用|

源快照保存进入处理前的原始业务字段。用户标识、发送日期、Cookie、完整网络响应在适配器入口丢弃。字幕原文件随版本存入私有 SQLite 数据库。重新分类生成新 run，可并排查看旧分类与终选。翻译、18 号小字号、证据不足或没有分类结果的弹幕默认不勾选。人工决定优先。重新分类后如需沿用旧选择，显式按同一源条目复制成新修订。

**时间轴假设**：首版按源片与重制片等长同剪辑设计，源分 P 的零点对齐目标分 P 与字幕。审核可设置逐条发送偏移，字幕证据仍按源时间定位。创建发送批次前显示目标、时长、样例字幕与弹幕；越界条目列明并阻止入批。工作台显示视频时长、时长差和时间轴位置时向上取整到秒。时长差异判定、越界检查和投递位置使用原始毫秒值。剪辑变动需要后续加入可版本化映射，时长相同本身不足以证明对齐。效果适配优先保留原值，无法表示的模式默认跳过并显示原因。

分析一次提交所选语言的完整字幕和快照内全部弹幕。模型请求中未选语言的字幕数组为空。构建模型请求时，中文字幕若为 ASS 或按 ASS 解析的 TXT，纯文本、起始时间及结束时间均相同的对白仅发送原始顺序中的第一条，沿用该条的字幕 ID。每条弹幕包含相对时间、匿名条目 ID、文本和可选的字幕提示 `subtitle_track_cue_id`。字幕 ID 按语言使用 `ja:`、`zh:` 前缀。字幕和弹幕作为引用数据，模型工具调用关闭。运行使用冻结的快照及版本，原弹幕内容和显示属性保持原样。

`translation` 表示主要在翻译日语台词，包含意译、部分翻译和明显的误译。`other` 包含讨论、吐槽、玩梗、问候、纯标点和引述台词以发表评论。提示模板第 5、6 版将涉及字幕或翻译话题的 `other` 条目标记为 `subtitle_or_translation_topic`，包括只在部分内容中谈及该话题的弹幕。第 4 版使用 `translation_request` 标记因缺少字幕而求翻译、呼唤字幕提供者或感叹的弹幕。第 4 至 6 版均在存在具体翻译迹象、结合字幕和语境后仍难以判断时标记 `needs_review`，并由后端将纯标点弹幕归为普通 `other`。带有诊断标记的条目默认取消勾选。

第 6 版要求把一组弹幕按时间顺序对照多句台词，并独立判断同一句台词的各份译稿。请求准备时，后端用已选中文字幕检查同位置、同字号、同色的固定弹幕。文字经 NFKC 归一化、大小写折叠并保留字母和数字后，至少 5 字且完全匹配、弹幕与字幕起点相差不超过 1.25 秒的条目参与检查。45 秒内至少匹配三个不同起点和三段不同文本的序列时，通过 `subtitle_track_cue_id` 提供对应中文对白。模型仍判为普通 `other` 时，后端保留类别并加上 `needs_review`、原因和对应字幕。此检查使用已有输入，无额外请求或重试。审核页用琥珀色整行突出待核对条目。

单行审核数据如下。

```json
{
  "item_id": "i23", "time_ms": 15420, "text": "原来她刚才已经发现了",
  "color_rgb": 16777215, "font_size": 25, "mode": 1,
  "classification": {"category":"other","reason":"评论角色行为","cue_ids":["zh:c8"],"diagnostic":null},
  "nearby_cues": [{"id":"zh:c8","start_ms":14900,"end_ms":16600,"plaintext":"你早就知道了？"}],
  "selected": true, "selection_source": "suggestion"
}
```

人工操作后 selection_source 为 manual。classification 绑定查询指定的 run_id；每条结果的关联 cue_id 仅引用本次分析使用的字幕版本。

## REST 契约

统一前缀 `/api/v1`。已发布接口见本地 OpenAPI。JSON 字段 snake_case。列表返回 `{items,next_cursor}`；游标绑定排序与筛选，limit 默认 100、最大 500。资源不可见按 404。变更成功后返回提交结果，任务创建和控制返回 202；用例错误形如 `{"error":{"code":"revision_conflict","message":"审核版本已更新","retryable":false}}`。400 游标错误、401 会话失效、403 权限、409 冲突、413 上传过大、422 字段错误、503 暂不可用。所有校验错误也需脱敏。异步失败记录在 Task.error。

|方法和路径|请求|成功响应|
|---|---|---|
|GET /health|无|200 {"status":"ok","api_version":"v1"}|
|POST /session、GET /session、DELETE /session|无正文。POST 需要同源 Origin、回环来源和 `X-Danmaku-Local: 1`。DELETE 需要 CSRF|POST 201 打开或复用会话并返回 csrf_token，GET 200 返回现有 csrf_token，DELETE 204 撤销会话|
|PUT /settings、GET /settings|GET 无正文。设置页只提交有改动的 llm、review、sending。`review.uncheck_small_by_default` 默认为 true。关闭后，字号 18 的弹幕按原有分类建议勾选，人工选择优先，跳过分析时仍默认全选。变更设置会更新审核修订，旧预览需重新确认。`sending.dual_account_enabled` 默认为 false，单独提交开关时保留发送间隔，省略开关时保留原值。兼容客户端仍可传只写 bilibili|200 {bilibili,llm,review,sending}，含配置和主账号凭据状态|
|PUT /settings/bilibili、GET /settings/bilibili|GET 无正文。兼容客户端可用 PUT 传只写 credential（sessdata、bili_jct、可选 buvid3）|200 {configured,version,account_id}|
|POST /settings/bilibili/check-credential|兼容客户端验证已保存登录，无正文|200 {status}，值为 unconfigured、valid 或 renewal_required。验证受阻返回 403，服务或凭据存储异常返回 503|
|POST /bilibili/login/status|本机会话与 CSRF，无正文。所有登录接口都接受 `role=primary` 或 `role=secondary` 查询参数，默认 primary。工作台定期检查主账号，并在双账号投递启用或在设置中勾选时检查次账号。次账号首次检查使用 `force=true` 跳过缓存，仅在存在已保存凭据时请求 B 站|200 {status,profile,auto_refresh}。status 为 unconfigured、valid、renewal_required 或 unavailable。profile 提供 UID、头像、昵称和 `level`，等级未知时为 `null`|
|POST /bilibili/login/qr|本机会话与 CSRF，无正文。创建或替换指定账号角色的扫码票据|201 {id,image}，image 为 PNG data URL，id 为本地轮询引用|
|POST /bilibili/login/qr/{attempt_id}/poll|本机会话与 CSRF，无正文，最长等待 3 分钟。轮询须使用创建票据时的角色|200 {status,login}。等待、已扫码、过期、登录成功分别为 waiting、scanned、expired、authenticated。成功时附登录状态。同一 UID 不能同时绑定两个角色，返回 409 `bilibili_duplicate_account`，换账号后重新扫码。临时上游故障返回 503，客户端可稍后轮询|
|DELETE /bilibili/login/qr/{attempt_id}、DELETE /bilibili/login/|本机会话与 CSRF，无正文。取消指定角色的票据或注销指定角色的账号|204，重复请求成功。后端会尝试撤销该 B 站会话，本地绑定已清除时无需等上游恢复|
|GET /send-capabilities|无正文|200 以 live 字段返回模式、字号、RGB、特殊颜色和平台实测状态|
|PUT /settings/llm；GET /settings/llm|PUT 传 provider、base_url、model，可选 thinking_enabled、reasoning_effort（low/high/max）、extra_requirements、只写 api_key 或 remove_api_key。GET 无正文|200 模型配置、configured、version、can_set_reasoning_effort，无密钥。can_set_reasoning_effort 表示当前配置支持调整思考强度，且已启用思考|
|PUT /settings/sending；GET /settings/sending|PUT 传 item_min_seconds、item_max_seconds、rate_limit_seconds，均为 0–86400 的整数秒，下限不能超过上限。省略字段用默认值 5、7、180。GET 无正文|200 返回当前三个字段，格式错误返回 422，数据库繁忙返回 503，可重试|
|GET /videos/{bvid}/parts|source 为 mock（默认）或 bilibili|200 VideoPart 数组，含 P、CID、毫秒片长 `duration_ms`、分 P 标题 `title` 和视频标题 `video_title`（可为 null）。B 站片长取对应 CID 的播放地址响应 `timelength`|
|POST /projects；GET /projects；GET /projects/{p}|工作台创建只传 `target_bvid`、`target_page`，使用查询参数 `source=bilibili` 冻结目标分 P。兼容已有客户端传入成对的 `source_bvid`、`source_page`，以及可选的 `name`|201 Project。单分 P 默认名称为目标视频标题，多分 P 默认名称为 `目标视频标题 - 目标分 P 标题`。缺失标题时用 BV 号和分 P 序号。只提供目标时，返回的源 BV 和分 P 为 null。兼容模式下，已解析的两侧分 P 时长差超过 1000 ms 时返回 409 `project_duration_mismatch`，可带 `confirm_duration_mismatch=true` 重试。GET 返回 200 项目列表或详情|
|PATCH /projects/{p}|{name}，去除首尾空白后须为 1–120 字符，需项目所有者会话与 CSRF 校验|200 更新后的 Project；404 项目不存在或不属于当前用户；422 名称无效；503 数据库繁忙时可重试，重复提交同一名称结果相同|
|DELETE /projects/{p}|项目 ID，需会话与 CSRF 校验|204 单事务永久删除项目及其本地分 P、快照、字幕、任务、分析、审核和投递记录；404 项目不存在、已删除或属于其他用户；409 `project_tasks_active`，等待活动任务结束或暂停后重试；503 数据库繁忙时可重试。已发到平台的弹幕仍保留|
|GET /projects/{p}/inputs|无|200 {parts,snapshots,subtitles,workflow,send_in_queue}。分 P 元数据可以为空。快照含名称、状态、覆盖范围和合并来源。workflow 保存已选快照、日中字幕版本、当前分析和已确认的审核修订。当前项目有批次占用投递队列时，`send_in_queue` 为 true，包括排队、暂停、退避和取消结算期间。工作台据此禁用发送预览的提交操作|
|GET /projects/{p}/snapshots/{s}/items|可选 cursor、limit|200 按时间与条目键稳定分页，条目含时间、原文、RGB、字号和模式。404 快照不存在，409 快照未完成，400 游标无效或用于另一快照|
|POST /projects/{p}/snapshots/merge|{snapshot_ids,confirm_duration_mismatch}，必须包含当前所有已完成的原始快照，至少两份|200 新快照或相同来源组合已有的合并快照。原始快照和重复弹幕保留。来源集合改变时返回 409 `snapshot_set_changed`，刷新后重试。最大和最小时长相差超过 1000 ms 时返回 409 `snapshot_duration_mismatch`，确认后带相同成员重试|
|PUT /projects/{p}/workflow/snapshot|{snapshot_id}，选择本项目的完整快照|200 Workflow。改选快照会重置当前分析与审核确认。404 快照不存在，409 快照未完成|
|PUT /projects/{p}/workflow/review|{run_id,revision}，确认已完成分析或已跳过分析的当前审核修订|200 Workflow。运行未完成时返回 409 `analysis_required`，版本变化时返回 409 `revision_conflict`，刷新审核后重新确认|
|DELETE /projects/{p}/workflow/subtitles/{language}|language 为 japanese 或 chinese|200 Workflow。清除该语言已选字幕时重置当前分析与审核确认。重复请求返回当前 Workflow，字幕历史版本仍供既有分析引用|
|POST /projects/{p}/subtitles|application/octet-stream 原始文件，查询参数 format 为 ass、srt 或 txt（TXT 须包含 ASS 或 SRT 字幕），可选 encoding、filename、language（japanese 或 chinese）。SRT 与 TXT 在默认 UTF-8 选项下识别 UTF-16 BOM|201 SubtitleVersion，encoding 为实际使用的编码。含 SRT 字幕的 TXT 返回 format=srt，保留原文件名和字节。SRT 对白移除常见样式标签并保留换行。对白可在片尾后结束，保留原结束时间。指定语言时，原子保存字幕版本及该语言的当前选择，并重置当前分析与审核确认|
|DELETE /projects/{p}/subtitles/{v}|项目 ID 和字幕版本 ID，需项目所有者会话与 CSRF 校验|204 原子删除未被分析引用的字幕原文件和对白；404 项目不属于当前用户或项目/版本不存在（包括重复删除）；409 `subtitle_in_use`，版本被任一日中字幕分析引用；503 数据库繁忙时可重试|
|POST /projects/{p}/tasks|见下方判别联合，Idempotency-Key 必填|202 Task；Location 指向状态 URL。同一项目已有批次占用投递队列时，新投递请求返回 409 `send_active`，批次退出队列后可重试。匹配幂等键和原请求的重放仍返回原任务|
|GET /projects/{p}/tasks；GET /tasks/{t}|可按 kind、state 过滤列表|200 按创建顺序倒序分页或 Task|
|DELETE /tasks/{t}|任务 ID，需会话与 CSRF 校验|204 单事务删除任务、事件和关联的本地结果；404 任务不存在、已删除或不可见；409 `task_active`（运行中、正在暂停或正在取消投递）或 `task_in_use`（被下游任务引用），解除冲突后可重试；503 数据库繁忙时可重试。已确认投递保留去重键|
|GET /projects/{p}/runs；GET /projects/{p}/runs/{r}|列表支持分页；详情传 run ID|200 分页或 AnalysisRun，含名称、固定快照、字幕、模型配置、上下文策略、提示版本和任务状态。跳过分析返回 `strategy=skipped`，模型配置和提示版本均为 `null`|
|GET /projects/{p}/runs/{r}/classifications|cursor、limit|200 按源条目键排序的结果分页。每条含 id、category、reason、cue_ids、diagnostic|
|GET /projects/{p}/review|run_id、category、selected、q、cursor、limit|200 {items,next_cursor,revision,selected_count}；每行含条目、text_edited（是否有已保存的改写）、offset_ms（默认 0）、checkbox_type、classification、相邻 cue、模型引用 cue、selected、选择来源|
|PATCH /projects/{p}/review|{run_id,expected_revision,item_ids,selected}，单条亦传数组；每批上限 500|200 {revision,changed_count,selected_count}|
|PATCH /projects/{p}/review/offset|{run_id,expected_revision,item_id,offset_ms,apply_to_following}，offset_ms 为带符号的安全整数毫秒|200 {revision,changed_count,selected_count}，未知条目返回 404，修订冲突返回 409|
|PATCH /projects/{p}/review/text|{run_id,expected_revision,item_id,text}，字符串须含非空白字符，null 表示撤销改写并恢复源文本|200 {revision,changed_count,selected_count}，未知条目返回 404，修订冲突返回 409，空白文本返回 422。冲突后重新读取审核数据，核对后可重试|
|POST /projects/{p}/review/reset|{run_id,expected_revision}，恢复该运行所有条目的默认建议|200 {revision,changed_count,selected_count}|
|GET /projects/{p}/send-preview|run_id、mode（默认 live，仅接受 live）、cursor、limit（默认 5）|200 含审核修订、源和目标分 P、整运行已选数、越界数、效果不支持数（`unsupported_count`）、真实投递记录数、缺少稳定 ID 的数量、条目分页及字幕样例。`source_parts` 按该运行的合并快照成员顺序返回各源分 P，单一快照返回含 `source` 的数组。`source` 提供该运行用于判断时长差的源分 P。分 P 时长差严格超过 1000 ms 时，`duration_mismatch_message` 包含两侧时长及差值，工作台据此在提交前询问是否继续。其他情况该字段为 `null`。条目提供 `checkbox_type` 与 `account_role`。可投递条目的角色为 `primary` 或 `secondary`，预检失败、去重跳过或无法创建批次时为 `null`。启用的次账号通过 `secondary_account_id` 与 `secondary_level` 返回，其余情况两字段为 `null`|
|POST /tasks/{t}/pause；POST /tasks/{t}/resume|{expected_version}|202 Task；重复请求已达目标返回当前状态|
|POST /tasks/{t}/send-primary-only|会话和 CSRF，无正文|202 返回最新任务快照。当前批次停止给次账号分配条目，在途请求照常结算，后续按原主账号的冷却调度。重复切换返回已有快照，任务结束或正在取消时返回 409 `send_primary_only_unavailable`|
|GET /send-queue/dual-upgrade|会话|200 返回当前可切换为双账号的队列任务数|
|POST /send-queue/dual-upgrade|会话和 CSRF，`secondary_account_id` 为用户确认的次账号 UID|202 返回本次接受切换的任务数。次账号不再可用时返回 409 `send_accounts_changed`。重复提交已接受的切换不产生新请求|
|GET /send-queue/active|会话|200 当前会话可发起插队时返回队首任务 ID，否则返回 null。允许状态为 running、backoff、pausing 和 paused。已暂停时可提交新的插队请求，其余状态只接受一个待执行请求|
|GET /send-queue/interrupted|会话|200 可批量恢复的投递批次数量。工作台在数量大于 1 时显示“恢复所有任务”|
|GET /send-queue/connection|会话|200 返回布尔值，表示后端能否经 HTTPS 连接 B 站 API，首次探测完成前返回 null。响应禁止缓存，工作台每秒读取，断线时以警示色显示“网络未连接”|
|GET /send-queue/rate|会话|200 返回 `{task_id,items_per_hour}` 或 `null`。队首须属于当前主账号且处于 running、backoff 或 pausing，已开始且未被插队挂起的 queued 队首也计入。按仍参与投递的账号分别计算速率并求和，回退至主账号时排除次账号。每个账号使用最近最多 1000 条确认记录，按相邻确认发送的间隔数除以累计的常规等待和限流等待，包含跨项目等待，重叠等待只计一次。样本不足 100 条或等待总量为零时使用配置的每条平均间隔估算。无符合条件的队首、无参与的账号或有账号无法估算（配置间隔为零且无可用历史速率）时返回 `null`。工作台在投递控制成功或进度显示参与账号变化时刷新，并在每次读取完成后等待 5 秒再刷新。页面隐藏时跳过读取，临时失败时下次刷新重试。发送进度以本批次尚待投递和在途的条数除以合计速度，显示预计剩余时间|
|GET /send-queue/paused-head|会话|200 人为暂停的队首任务，含 `task_id`、`expected_version` 和 `project_name`。没有符合条件的任务时返回 null。点击“恢复所有任务”时读取|
|POST /send-queue/resume-interrupted|会话和 CSRF，可附已确认队首的 `task_id` 和 `expected_version`|202 本次恢复的投递批次数量。在同一写事务中恢复中断批次和已确认的队首，保留队列位置、回执、进度和冷却期限。取消提示时仍恢复中断批次。已确认的队首或版本变化时返回 409，请重新发起。无正文重复提交返回 0，数据库忙时返回 503，可重试|
|POST /tasks/{t}/send-priority|{expected_version, confirmed_preload_task_id?}，需会话与 CSRF 校验|202 排队批次 Task。该批次与发送中或暂停的队首互换位置。有在途弹幕时，返回后继续等待结算。队首为预装批次时，缺少该批次 ID 的确认返回 409 preload_priority_confirmation。批次不可见返回 404，版本不符返回 409 version_conflict，其他状态冲突返回 409 send_priority_unavailable。数据库忙返回 503，可重试|
|GET /preload/parts|已建立本机会话且有有效的主账号登录|200，返回 {part, publish_at, already_created} 数组。无符合条件的稿件时为空数组。主账号登录失效返回 409 bilibili_credential_rejected，读取失败返回可重试的 502 或 503|
|POST /preload/projects|{bvid, page}，需会话与 CSRF 校验|201 Project。后端重新核实稿件、分 P 和公开时间。同一 BV 和分 P 已有项目返回 409 preload_project_exists，稿件或分 P 已更新返回 409 preload_archive_changed|
|GET /preload/overdue|已建立本机会话|200，返回待确认的最先过期任务 ID 或 null|
|POST /preload/overdue|{task_id, send}，需会话与 CSRF 校验|204。确认发送前重新核实主账号登录，拒绝则撤销全部等待公开的计划批次。待确认队首变化返回 409 publication_queue_changed，已处理的决定可重放|
|POST /tasks/{t}/send-cancel|{expected_version}，需会话与 CSRF 校验|202 Task。原子撤销批次内全部待投递条目。若仍有在途弹幕，先进入 `cancelling`，结算回执后进入 `cancelled` 并释放队列位置。重复取消直接返回当前状态，其他版本冲突返回 409 `version_conflict`，刷新后可重试。不可见或非投递任务返回 404，已结束或非真实批次返回 409 `send_cancel_unavailable`。数据库忙返回 503，可重试|
|POST /tasks/{t}/stop|仅用于分类任务，需 CSRF，无正文|202 Task。未提交结果的运行标记为 failed（error.code 为 analysis_stopped），重复停止返回当前记录；非分类任务返回 404|
|GET /tasks/{t}/deliveries|status、account_role（可选 `primary` 或 `secondary`）、limit，cursor 与从 1 开始的 page 二选一，也可均省略。`status=active` 筛选所有待投递和发送中条目，按发送中、已错过原位置的重发、其余待投递分组，每组按原顺序排列。`status=withdrawn`、`status=skipped` 和 `status=failed` 各自包含从对应分类重新排队或正在发送的条目。可重复传入 `track`（最多 10 个 12 位明细 ID）|200 {items,next_cursor,batch,task_version,total_count,tracked_items}，total_count 为当前状态和账号筛选的总条数，page 超出范围时返回最后一页。游标按批次、状态和账号角色限定。待投递条目按当前分配角色筛选，批次切至仅主账号后，待投递条目均归主账号。其余条目优先使用最近一次实际尝试的角色，再使用条目分配角色。无角色的条目归主账号。`batch.account_id` 和 `batch.secondary_account_id` 为冻结的主次账号 UID，单账号批次的次账号 UID 为 `null`。`tracked_items` 返回同一批次中由 `track` 指定且未出现在 `items` 中的条目，独立于筛选和分页计数。明细含冻结条目、效果差异、回执、尝试记录和 `resending` 标记。前端使用追踪结果完成发送动画后移出卡片。条目的 `item.account_role` 为保存的分配角色，待投递条目发生分担时随之更新。尝试记录的 `account_role` 为实际发送角色，旧记录可为空。条目和尝试记录均提供 `error_code` 与可为空的 `bilibili_error`（`http_status`、可为空的 `code` 与 `message`）|
|POST /tasks/{t}/deliveries/{d}/resolve|{expected_version,outcome}，工作台提供 confirmed 和 skip|200 Delivery，记录操作者。未知条目在发送中也可确认或跳过。确认成功后，同项目有排队批次时应用“插队发送”的队列规则。仅因未知回执暂停且尚有待投递条目的批次会恢复排队。兼容旧客户端的 retry 在已暂停或 `needs_review` 批次可用，已取消批次返回 409 `delivery_retry_cancelled`|
|POST /tasks/{t}/deliveries/{d}/withdraw|{expected_version}，需会话与 CSRF 校验|200 Task。仅能撤销真实批次中尚待投递的条目，增加 `progress.withdrawn` 和 `progress.done`，保留原条目。全部处理后按正常规则结算批次，项目仍可自动完成。状态不符返回 409 `delivery_withdraw_unavailable`|
|POST /tasks/{t}/deliveries/{d}/resend|{expected_version}，需会话与 CSRF 校验，工作台在提交前询问确认|202 Task。使用原批次的账号、目标和内容，将已撤销、已跳过或失败的条目重新排队，扣回对应的分类与已处理计数，并将不支持的字号调整为 25。逐条重发失败弹幕会重新计算该条的限流重试预算。条目回到原顺序。已有后序条目开始过发送时，重发排到待投递队首，等当前在途条目结束及其必要重试完成再发送。多个优先重发按原顺序排列。出队时仍检查效果、来源键和已确认回执。已结束批次进入队尾，已暂停批次恢复，继续遵守账号冷却。条目不适用或批次正在暂停时返回 409 `delivery_resend_unavailable`，有未知回执时返回 409 `delivery_unknown`|
|GET /tasks/{t}/events|EventSource，Last-Event-ID 或初次 after 查询参数。分类任务可传 analysis_console=true|200 text/event-stream。启用分析终端时增加 analysis.console 和 analysis.console.end 事件|

撤销与重新发送各自使用单个写事务更新条目、任务版本和统计。条目不存在返回 404，版本不匹配返回 409 `version_conflict`。发生版本冲突后应先刷新明细，再决定是否重试。数据库忙时返回 503，可以重试。旧版本投递记录的 `progress.withdrawn` 和 `resending` 分别默认为 0 和 false。

任务创建的 kind 区分下载、分类和投递。

```json
{"kind":"download","mode":"bilibili","source_bvid":"BV0000000001","source_page":1}
{"kind":"download","mode":"bilibili","source_cid":"9007199254740993"}
{"kind":"classify","snapshot_id":"s1","subtitle_version_id":"zh1","japanese_subtitle_version_id":"ja1","llm_config_version":2}
{"kind":"classify","snapshot_id":"s1","skip_analysis":true}
{"kind":"send","run_id":"r1","expected_review_revision":6,"confirmation":{"target_cid":"502","account_id":"103","secondary_account_id":"104","secondary_level":3}}
```

下载命令冻结源 BV 和分 P，或冻结 CID。CID 为 1 至 20 位正整数字符串，仅支持 `bilibili` 模式，且与源 BV 和分 P 互斥。未传源视频时沿用旧项目的源引用，两处均缺少时返回 422 `source_required`。下载模式默认 mock。工作台默认选择 BV，切换 CID 后直接提交下载。原始快照按下载完成顺序取得项目内编号，失败后仍可使用已有完整快照。合并在单次 SQLite 写事务中保存新快照和复制条目，副本具有独立的预览和分析条目键，发送账本继续使用原始平台 ID。相同来源组合的重试返回原合并快照。合并比较片长已知的来源，全部来源片长已知时使用最长片长。数据库忙时可按 503 重试。

SQLite v15 新增 `project_workflows` 并为已有完整快照补编号，为已有分析的项目恢复最近一次分析及对应的字幕和快照选择。升级前备份数据库。工作台按拉取、分析、审核、发送划分四步，活动任务和发送回执经任务接口读取。审核确认绑定项目审核修订，修订变化后须再次确认。工作台的发送预览展示弹幕，HTTP 预览仍返回字幕样例以兼容已有调用方。

智能分析接受完整快照、模型配置版本及同项目下至少一份含对白的字幕。`subtitle_version_id` 指中文字幕，`japanese_subtitle_version_id` 指日语原文字幕。未选语言可省略或设为 `null`。两项都未选时返回 422 `subtitle_required`，两项使用同一版本时返回 422 `subtitle_versions_match`。每次智能分析只提交一次模型请求。请求失败后可手动发起新的分析。每次运行独立保存，名称为“弹幕快照名称分析”。

智能分析可提交 `reasoning_effort`（`low`、`high`、`max`），省略或传 `null` 时使用指定版本的全局默认强度。当前实现支持启用了思考的官方 DeepSeek Flash 配置，其他配置收到具体强度时返回 422 `reasoning_effort_unsupported`。跳过分析时不接受具体强度。任务创建时冻结实际使用的强度，恢复任务沿用该值。支持调整强度的分析提交成功后，项目 `workflow.reasoning_effort` 保存所选值（`null` 表示跟随全局）。跳过分析或使用不支持调整强度的模型时保留项目已有选择。

`skip_analysis=true` 接受完整快照和可选的同项目字幕，省略 `llm_config_version`。单个写事务保存运行与工作流，返回 `state=succeeded`、`checkpoint.analysis_skipped=true` 的任务。此流程没有模型请求或分类结果。工作台确认后直接打开人工审核，快照内所有条目默认勾选，审核重置恢复全选。仍可参考提交时已选的字幕。SQLite v20 升级前备份数据库，允许无需字幕的运行并保留原有运行和下游记录。

分类提交、跳过和恢复在写事务中互斥，已存在活动分析时提交返回 409 `analysis_active`。数据库忙时可按 503 重试。投递事务校验审核版本、已选数量、目标及模式，然后物化批次。投递仅支持 live（默认），必须附上目标 CID 和主账号 UID 确认。工作台在 `confirmation` 中附上预览的 `secondary_account_id` 和 `secondary_level`，包括单账号时的两个 `null`。与当前次账号配置不一致时返回 409 `send_accounts_changed`，需重新读取预览并确认。未提供这两个字段的历史客户端按提交时的配置分配。预览选择「使用主账号发送」时，客户端发送 `confirmation.primary_only: true` 并将次账号 UID 和等级设为 `null`，该批次仅使用已确认的主账号。提交或预览指定 mock 返回 422。同主体、同路径、同幂等键提交等价命令返回原任务，不同命令返回 409。

```json
{"id":"t1","project_id":"p1","kind":"send","state":"paused","version":7,
 "progress":{"done":12,"total":40,"failed":1,"skipped":0,"unknown":1},
 "checkpoint":{"batch_id":"b1","started":true},"next_attempt_at":null,
 "error":{"code":"delivery_unknown","message":"有一条弹幕需要核对","retryable":false}}
```

done 统计已终结条目（含明确失败和跳过），unknown 单列、等待处理。服务端按 (time_ms,item_id) 稳定翻页。批量勾选作用于明确列出的 ID。UI 显示筛选结果与当前运行的已选数，发送面向整个修订。桌面审核布局采用左侧筛选、中间可连续滚动的表格、右侧同期字幕和模型依据。设置页管理模型密钥、分析参数和投递间隔，监控页显示退避倒计时、失败与待核对项。

## 状态机和故障边界

|当前状态|触发条件|新状态 / 副作用|
|---|---|---|
|queued|worker 原子认领；用户暂停|running；paused|
|running|确定可重试的瞬态错误 / 限流 / 真实投递间隔|backoff，存尝试数及下次执行时间|
|backoff|到期；用户暂停|真实投递队首直接进入 running，其他任务进入 queued；paused|
|running|请求暂停|pausing，停止领新项，在途调用结算|
|pausing|在途调用结束并保存检查点|paused|
|running|凭据失效|paused，写阻塞原因|
|running|投递结果未知|该条进入 `unknown`，其余待投递条目在正常发送间隔后继续|
|paused|阻塞项已解决，用户恢复|已开始的真实投递队首仍在账号冷却期时恢复到 backoff，并返回冷却截止时间和限流或延长冷却原因。其余进入 queued，从检查点续作，仍遵守退避截止时间|
|running|所有条目终结|succeeded，或有明确失败时 failed，保留部分成功|
|running / backoff|任务级永久错误或任务级重试预算耗尽|failed|
|未结束的真实投递|取消该投递|撤销全部待投递条目，进入 `cancelled`，有在途条目时先进入 `cancelling`|
|cancelling|在途调用结算或进程重启|进入 `cancelled` 并释放队列位置。保留确认回执，明确未送达的在途条目归入撤销，未知结果保留待核对|
|`needs_review`|已无待投递条目且仍有未知回执|释放队列位置。核对完毕后进入 `succeeded` 或 `failed`，手动重发时进入 `queued`|
|succeeded / failed / cancelled|终态|保留任务历史|

单进程 worker 最多并行执行四个下载任务。分析使用独立调度，认领后立即启动，应用不设全局分析并发上限。同一项目的同类任务逐个执行。真实投递使用独立的全局队列。SQLite v10 在任务创建的同一事务中写入队列位置，重复提交复用原任务。只有队首可被认领。队首退避或暂停时保留原位置，恢复后继续执行，已终结批次的失败重试排到队尾。

插队只交换队首与指定排队批次的两个位置。已有在途弹幕时，将目标写入队首任务的 `checkpoint.next_send_task_id`。单账号在当前条目确认成功或明确失败后交换。双账号在有条目确认成功或明确失败，且所有其他在途请求已经结算后交换。如果所有在途条目均因限流回到待发送，队首继续调度直至有条目结算。出现未知回执、平台阻止发送或目标批次已离开排队状态时，清除请求。进程中断后的在途弹幕沿用未知回执核对流程。交换后的原任务以 `checkpoint.waiting_after_priority` 标记排队状态，保留进度与按 UID 保存的冷却期限，重新领取时清除标记。插队批次继续遵守账号冷却。

已暂停的队首可直接交换位置，并在新位置保持暂停。有在途弹幕的队首进入正在暂停后仍可接受插队。结算和交换完成后，原批次在新位置进入暂停，继续保留原回执和人工核对状态。

投递任务终结后，worker 可以认领下一批，发送仍遵守账号和条目级退避。等待失败处理决定的批次在选择“继续下一批”后终结。SQLite v18 在升级前备份旧库，并移除投递队列等待表及批次间隔设置。旧的 30 秒限流重试设置采用新的 180 秒默认值，其余限流设置继续生效。已有队列位置继续有效，从 v9 或更早升级时按创建顺序补入未终结投递。

SQLite v12 将项目审核修订单独持久化。升级时备份旧库并从已有记录回填修订，删除分析任务及其审核记录后修订仍继续递增。

显式限流优先遵守服务端等待时间，否则指数退避加抖动、设间隔上限和总尝试预算。同账号共享持久化的冷却截止时间。客户端断开后任务继续运行。SQLite 写事务原子认领任务，以版本条件防止重复控制。任务变更、队列变更、条目结果、检查点和 SSE 事件同事务提交。

发送在网络调用之前提交 in_flight 记录，确认后落为 confirmed。超时、响应丢失或重启时，在途条目转为 unknown，等待人工核对。运行中的批次继续处理其余条目，正在取消的批次完成取消。重启时仍有待投递条目的批次进入暂停，可直接恢复其余条目。真实投递按项目、目标 CID 和稳定源条目键查询已确认记录，跨 run 和任务生效。下载从已保存进度续作，分析按完整请求恢复。

批量恢复根据任务事件判定中断前的状态。原先等待重试的批次恢复退避状态和原因，原先排队或运行的批次进入排队并由 worker 认领。主动暂停、取消、回执待核对或等待失败处理决定的批次继续遵守各自状态流转。

选择 **SSE**：控制命令已有 REST，浏览器只需接收进度。刷新先 GET 快照，再从该快照 version 回放（每次任务提交发布一个事件，seq = version）。事件名 task.updated，id 为 seq，data 为 Task；心跳用 SSE 注释，代理关闭缓冲。历史过期发 reset 及最新快照；客户端丢弃旧版本，断线期间可轮询 GET，终态关闭流。

## 阶段 1 接口与运行边界

- 项目存储源、目标的 BV 号和分 P。创建返回 `id`、`creation_order` 和提交字段，详情另带 `snapshot_ids`、`subtitle_version_ids`、`run_ids`，阶段 1 均为空。`GET /projects` 按 `creation_order` 从新到旧分页。游标绑定本机所有者及当前排序，旧游标返回 `invalid_cursor`，重新读取第一页即可继续。SQLite v16 升级前备份数据库，按旧项目表的插入顺序补项目创建序号。新项目在同一事务中分配递增序号，删除项目时清理其序号记录。项目授权以本机所有者为主体，任务详情和事件继承项目授权。
- 项目任务列表按 `creation_order` 倒序分页，最新创建的任务排在最上面。`creation_order` 取自 SQLite 已保存的任务创建序号，已有任务与历史事件均可读取。分页游标绑定项目和筛选条件，新游标以创建序号为界，删除边界任务后仍可翻页。旧排序的游标返回 `invalid_cursor`，刷新后从第一页重新读取。
- `POST /projects/{project_id}/tasks` 接收 `{"kind":"download"}`，幂等键为 1–128 个可打印 ASCII 字符，不含空格。创建捕获源 BV 号、分 P 和创建人。合成执行器接收任务记录，每成功一批提交五条计数，十批结束，检查点为 `{"completed_batches":n}`。它可抛出带脱敏错误信息和可选等待时间的批次异常。可重试错误每批至多尝试四次，已提交批次在重启后继续累计。在途永久错误遇到暂停时随检查点保存，恢复时结算为失败。
- 任务快照与对应事件同事务写入。重复暂停或恢复若已达相应状态，则返回当前快照。新转换以版本条件更新。SSE 的 `Last-Event-ID` 优先于 `after`，阶段 1 保存全部事件，游标超前或历史不连续时发 `reset`。
- 数据目录由 `DANMAKU_DATA_DIR` 指定，默认 `data/`。SQLite 的第 1 版包含项目、任务和事件表。读事务使用 `BEGIN`，写事务以 `BEGIN IMMEDIATE` 串行化，锁等待用尽返回可重试的 503。SQLAlchemy 显式事务的连接配置参照所用 2.0 系列文档。[SQLAlchemy SQLite 事务](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#transactions-with-sqlite-and-the-sqlite3-driver)
- 阶段 1 使用配对码建立本机会话。当前会话入口及边界见阶段 6。自动测试可注入内存凭据库。

## 阶段 2 输入与恢复边界

- 项目详情返回已提交的快照及字幕版本 ID。创建时传 `source=bilibili` 或 `source=mock` 会同时解析源和目标分 P，按 1 起的 P 号校验，并将 CID 和片长与项目同事务写入。省略该参数时接受阶段 1 的项目正文。字幕上传验证时间戳格式及先后顺序。有目标片长时，开始时间须满足 `0 ≤ start_ms < 片长`。指定字幕语言时采用目标片长。未指定语言且已有源分 P 时，采用源和目标的较短片长。时间戳与覆盖边界以毫秒表示。
- 字幕以文件字节流上传。路由在读取时限制为 2 MiB，UTF-8 支持 BOM，也可指定 UTF-16（含 BOM）、GB18030 或 Shift JIS。对白按开始时间和原始顺序排序，保留样式、重叠和多行。ASS 使用 pysubs2 的 `SSAEvent` 提取纯文本，注释、绘图和空对白产生行级警告。原字节、文件指纹和对白原子提交。相同项目、格式、编码、解析器版本与文件字节对应同一字幕版本。
- 默认合成下载产生合成快照。B 站分 P 解析先取视频详情，再按 CID 逐个调用 `get_download_url`，用返回的 `timelength` 作为毫秒片长。整次解析沿用 20 秒超时，缺少有效片长按响应格式错误处理。已保存的项目和快照继续使用冻结的片长。B 站任务先持久化草稿与 CID，按冻结的片长规划连续的六分钟分段，并用固定 CID 和相同的 `from_seg`、`to_seg` 请求一段。草稿覆盖界限只随成功提交的分段前移，空段也算已取。快照完成表示已取完计划内的当前接口分段。覆盖时间描述请求范围，按日期查询的历史记录和特殊弹幕池未计入。真实适配可用性见路线图。
- CID 下载使用 `bilibili-api-python` 17.4.2 请求客户端，按 CID 从 `/x/v2/dm/web/view` 读取分段数，再逐段调用 `/x/v2/dm/web/seg.so`。计划分段数和每段进度随快照保存，恢复时继续请求首个未提交分段。缺少分段数时，任务沿用批次重试并报告 `bilibili_segment_count_unknown`。CID 快照及投递预览的源分 P 结构为 `{cid, duration_ms: null}`，覆盖时间记录已请求的分段范围。CID 来源跳过片长比较，投递仍校验目标视频时间轴。
- CID 分段请求未发送条件缓存头。分段接口的空响应体 HTTP 304 按空段推进，并继续读取后续计划分段。元数据请求和其他错误响应沿用原有错误处理。
- 条目白名单为平台弹幕 ID、文本、出现时间、颜色、字号和模式。下载分段入库前丢弃空字符串和纯空白弹幕，其他弹幕保留原始文本。特殊颜色与越界弹幕保留原值，规范化记录带诊断。条目优先按平台弹幕 ID 去重，缺失时按段和段内序号区分。条目、快照、检查点和任务事件同事务提交。SQLite v1 自动备份到 `workbench.sqlite3.v1.bak` 后迁移到 v2；已经开始的阶段 1 合成任务按原计数继续。
- B 站解析和抓取单次调用最多 20 秒。限流及瞬态失败由任务调度按原分段退避，每段最多四次尝试。权限拒绝、弹幕关闭和 SDK 缺失可通过脱敏错误码区分。下载区直接显示本地错误码和多行错误消息。分 P 请求或任务的错误消息可附可取得的 B 站 HTTP 状态、业务码、脱敏业务提示和异常类型。任务执行错误在已知时附源 BV、分 P 和出错步骤或分段。失败时保留已提交草稿；新下载生成新快照。SDK 的分段调用会包装部分上游异常，重试使用本地指数退避。

## 阶段 3 分析与恢复边界

- `GET /settings/llm` 初始返回可直接使用的 `mock` 配置 `synthetic-v1`（版本 0，`base_url: null`）。`PUT` 为当前主体追加配置版本，支持 `mock` 和 `openai_compatible`。后者接受 HTTPS 以及本机回环 HTTP，基础 URL 禁止凭据、查询参数与片段。API 密钥通过只写字段提交。SQLite 保存不透明引用，Windows 桌面使用系统凭据库，其他平台在当前阶段使用进程内存。兼容服务的 `configured` 表示对应凭据在本地可读。密钥按主体与完整基础 URL 绑定，省略密钥时沿用同一端点的凭据。更新同一端点的密钥后可恢复使用原模型版本的暂停任务。新端点须有独立凭据。传入 `remove_api_key: true` 删除对应端点的凭据引用并清理凭据库，且不能与 `api_key` 同时使用。右上角的设置界面提供官方 DeepSeek 的 `deepseek-flash` 选项。已创建的分析继续使用原模型配置版本。额外分析要求默认留空，非空时追加到系统提示，保留现有分类定义和 JSON 校验。
- 兼容适配器通过 HTTPX 向基础 URL 下的 `/chat/completions` 提交一次 JSON 请求，设置 `response_format: json_object`。关闭重定向和环境代理。整次调用超时 15 分钟，连接超时 10 秒。直连官方 `api.deepseek.com` 且使用 `deepseek-flash` 时，启用 SSE 流式响应并按配置发送 `thinking`。开启思考时同时发送 `reasoning_effort`。历史配置沿用开启思考与 `high` 强度。流式响应最多接收 64 MiB，思考和输出文本合计最多 2 MiB，收到 `finish_reason: stop` 和 `[DONE]` 后解析最终 JSON。其他兼容端点沿用非流式响应及 2 MiB 限制，终端在收到响应后显示可用的文本。所有兼容端点的上下文窗口和输出 token 上限由服务商决定。[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- 分析终端通过当前会话对所属分类任务订阅 `analysis_console=true` 的 SSE。服务每次发送 `analysis.console` 的 `{replace,reasoning,output}`，初连、重连和同任务重新开始模型调用时 `replace` 为 true，其余为追加文本。任务结束后发送 `analysis.console.end`。这些事件没有任务版本号，浏览器断线重连可重取当前缓冲区。终端仅在进程内保留进行中的调用与最近 8 次结束的尝试，重启后清空。已有数据库中的分类结果单独供审核读取。离线合成模型提供结果 JSON。
- `classify` 原子创建任务和分析运行，任务 ID 同时是 run ID。新运行使用提示模板 6，双语策略为 `bilingual`，单语策略为 `single_subtitle`。所选字幕和全部弹幕只组成一个请求。后端严格核对类别、证据、字幕引用及全部弹幕 ID，每条必须恰好返回一次。结果、进度和事件同事务写入，进度在等候期间保持 0，完整结果落库后一次更新到总数。空快照跳过模型请求。
- 分类提示要求响应顶层仅包含 `items`。如果响应顶层恰好包含 `items` 和 `"type": "json_object"`，后端先移除回显的 `type`，再执行完整校验。其他额外字段或无效分类结果会使整批失败。
- 连接超时、网络中断、限流及模型服务错误均直接结束当前运行。启动时将旧版本仍在自动重试队列中的分析标记为失败。缺密钥或密钥被服务商拒绝时暂停，更新同一端点的凭据后可恢复。分析面板展示本地错误码。收到 HTTP 回包时，错误消息附上状态码和可读取的脱敏 `error.code`、`error.type`、`error.param`、`error.message`；连接失败时附上异常类型和可用的系统或证书错误码。手动重试时清除上一次的错误提示。任务、事件和日志只保存脱敏诊断。
- 暂停允许当前请求完成并保存，已落库的结果恢复时直接结束。进程中断后，手动恢复会重新发起未落库的完整请求。模板 2 的双语运行继续使用原提示词，模板 1 的分析结果和人工决定仍可查询，可选择至少一份字幕创建新版运行。
- 停止分析先持久化终态，再中断进行中的本地模型请求。已经排队的任务停止后跳过模型请求，和停止并发返回的模型结果由同一任务事务判定是否提交。如果分类结果已先提交，停止返回当前状态。终端继续展示内存中已收到的文字，重新分析创建独立运行。

## 阶段 4 人工审核

- 已成功或失败的分析运行可以进入审核。失败运行中尚无分类结果的快照条目也可人工处理，`classification` 为 null。已有分类失败记录保留诊断。审核读取运行固定的快照、分类结果和字幕版本。重跑沿用相同输入时创建独立运行，旧运行及人工选择仍可通过运行选择器查看。
- 同期字幕（`nearby_cues`）按语言分别去重。毫秒级起止时间与解析后的纯文本均相同的对白，取原始顺序中的第一条，再按距离截取至多 12 条。保留同轴异文、时间不同的重复对白和跨语言对白。规则适用于 ASS、SRT、TXT 和历史运行。分类依据（`evidence_cues`）仍按原引用 ID 读取字幕。
- `q` 最长 200 字符，按弹幕文字做字面子串筛选。筛选可叠加，`selected` 取人工决定或默认建议。结果按 `(time_ms,item_id)` 排序，游标绑定项目、运行和筛选。`selected_count` 统计当前运行所有已选条目。每行至多附带前后 5 秒内最接近的 12 条对白，`evidence_cues` 含模型或后端字幕匹配所引用的对白。审核详情显示核对原因，列表突出显示 `needs_review` 行。审核列表可逐条设置偏移，失焦时确认是否覆盖当前条目及其后续条目。批量范围按原始 `(time_ms,item_id)` 确定，包含所有筛选结果以外的后续条目。偏移按分析运行保存，重置勾选时保留；有变化时原子更新审核修订。遇到修订冲突需重新加载审核后再提交，相同修订下无变化的请求保持修订不变。新版分析中，未分类、待人工核对、翻译弹幕及涉及字幕或翻译话题的弹幕默认不勾选。历史运行沿用当时的分类及其默认规则。18 号小字号弹幕在所有运行中默认不勾选，仍可手动勾选。
- 项目审核修订从 0 开始。服务在同一写事务里验证期望修订、运行和整个条目集合，追加操作者和 UTC 时间，重复 ID 合并。把建议值确认为人工值会产生决定，重复提交当前人工值则返回 0 条变更，修订保持原值。过期修订返回 409，混入不存在的条目返回 404，整批回滚。每个运行独立取其条目的最新人工决定，项目内各运行共享并发控制修订。
- 桌面审核沿服务端游标每批最多读取 500 条，把同一审核修订下的全部筛选结果显示在一个滚动框内。读取期间修订变化时可重试读取。单条勾选提交显式条目 ID。提交遇到修订冲突时重新读取并提示核对。SQLite v3 升级前备份为 `workbench.sqlite3.v3.bak`，v4 新增追加式审核决定表。
- 桌面审核在确认后重置当前运行的所有人工选择，包括筛选隐藏的条目。服务在同一写事务内验证期望修订，统计有人工选择的条目数，有变更时追加重置记录并递增项目修订。审核列表和投递预览忽略最近一次重置及之前的人工决定，已创建的投递批次仍使用提交时冻结的条目。重置无变更时保留原修订。过期修订返回 409，客户端刷新后需重新确认。SQLite v14 新增审核重置记录表。

## 统一设置保存

设置页面通过 `GET /settings` 读取同一快照，`PUT /settings` 提交已修改的区域。替换 B 站凭据时先验证 UID，再在同一 SQLite 事务中保存设置和秘密引用。事务失败时清理新建秘密，提交后清理被替换的秘密。字段无效或上游验证失败时保留原设置。数据库或上游暂不可用时可在恢复后重试。接口无自动重试，写入使用本机会话的 Origin 和 CSRF 校验。

## 阶段 5 模拟投递（历史）

旧模拟任务在升级时退出队列，未完成的任务标记为 `send_mode_removed`。历史批次、回执和尝试记录仍可查阅。

- 预览与创建都读取已结束的分析运行，使用整个运行的有效选择，包含人工决定和审核建议。预览列出固定的源、目标分 P、片长和第一条样例附近的字幕。有越界时分页列出所有越界条目，游标绑定审核修订。旧合成快照所属项目缺少分 P 元数据时，用目标 BV 和 P 派生合成目标。B 站快照要求项目已有通过 B 站来源解析的目标信息，缺失时返回 `target_part_missing`。发送时间为原始位置加偏移。源位置必须在原始源片范围内，发送时间必须满足 `0 ≤ 原始位置 + offset_ms < 目标片长`。提交以同一写事务比较项目审核修订并冻结目标及按照 `(发送时间,item_id)` 排序的选集。空选集和越界返回 409，批次 ID 与任务 ID 相同。后续审核修改只影响新批次。
- 模拟发送保留文本、整数毫秒位置、字号、RGB 和原模式。阶段 5 的模拟器支持模式 1、4、5、6 和普通 RGB，其余模式或特殊颜色在批内标为 `skipped`，记录全部不支持的属性。每个模拟批次使用专有逐项账本，可以用新的幂等键重新模拟旧条目。回执带 `mock:` 前缀。真实投递独立去重。
- `Sender.send(SendWork)` 接收冻结的目标 CID、条目、模拟账号引用和尝试序号，返回确认及不透明 UUID、明确拒绝、限流或未知。Worker 在调用前事务提交 `in_flight` 及事件，结算时原子更新尝试、投递状态、计数和任务事件。确认记入回执和 UTC 时间，拒绝记失败后继续处理其余条目。收到明确未投递的限流结果时优先采用返回的等待秒数（0 至 86,400），缺省时指数退避加抖动，每轮最多四次尝试。以项目所有者为模拟账号，共享持久冷却截止时间，重启以及暂停恢复后仍执行该截止时间。
- 10 秒超时、发送过程的非预期失败或启动时遗留的 `in_flight` 都转 `unknown`。`done` 统计 `confirmed`、`failed`、`skipped` 和 `withdrawn`，`unknown` 单独统计。用户以任务 `expected_version` 核对未知条目，`confirmed` 记录人工确认时间，`skip` 计入跳过，`retry` 回到 `pending` 并开启新的限流尝试预算。核对保留原尝试结果，附记决定、操作者和时间。主动暂停允许在途条目结算，恢复后继续处理待投递条目。明确失败仍沿用失败处理流程。
- SQLite v5 新增 `send_batches`、`send_deliveries`、`send_attempts` 和 `mock_send_cooldowns`，升级前备份为 `workbench.sqlite3.v4.bak`。任务页沿用 SSE 和断线轮询，显示倒计时、投递状态筛选、分页、冻结属性、回执及核对操作。

## 阶段 6 真实投递与 Windows 交付

- 扫码入口位于右上角。后端处理 B 站网页 Passport 协议，并在账号查询确认 UID 后保存凭据。SQLite 保存 UID、头像、昵称、版本和秘密引用。Cookie、刷新令牌和待确认的旧令牌存在当前 Windows 用户的系统凭据库。已有账号凭据继续可用，可扫码启用续期。批次冻结账号和目标 BV、P、CID。每项发送前核对该账号仍绑定且凭据可读取，更换为不同 UID 会暂停旧批次。更新同一账号的凭据后可手动恢复。查询响应不返回凭据。
- 设置页的「弹幕投递」分类管理次账号。SQLite v22 为次账号增加独立绑定，主次账号分别保存凭据引用、扫码票据和续期状态。关闭双账号开关时隐藏次账号头像并保留登录。保存凭据、扫码成功及定期校验登录时，从现有账号验证响应的 `level_info.current_level` 读取并持久化主次账号等级。已有绑定在下一次成功校验时补齐等级。已保存的开关开启、次账号有凭据且等级至少为 1 时启用双账号投递。等级为 0 时设置页提示答题转正。下载使用主账号。
- 真实投递按发送时间和条目 ID 排序分配。UP 主弹幕由主账号处理。次账号 Lv3 及以上时，剩余弹幕从主账号开始交替分配。Lv2 只分配普通滚动弹幕，Lv1 只分配普通白色滚动弹幕，次账号最多领取整个可发送批次的一半（奇数向下取整）。预检失败或去重跳过的条目无需分配。预览分页和提交都计算完整选集，提交事务保存次账号 UID 和逐条角色。重试和重启沿用最近一次已保存的分配，历史批次默认由主账号发送。初始分配保存在既有 JSON 列中。
- 发送队列保留队首优先。每个 UID 最多有一条在途弹幕，同一项目和目标 CID 的同一去重键最多有一个在途请求。两个账号共用发送、限流和重试规则，冷却和重试时间按实际 UID 保存。双账号批次的 `checkpoint.accounts` 通过任务详情和 SSE 返回两个角色的 `state`（`ready`、`sending`、`waiting` 或 `finished`）、`next_attempt_at` 和 `error`。批次退避时间取待发送账号中最早的尝试时间。双账号开关关闭、次账号退出或失去发送资格后，worker 在下一次调度检查时持久化 `checkpoint.primary_only`，将批次后续待发送条目交给原主账号，并按其冷却期限调度。次账号在途请求沿用原账号结算，未知回执仍需核对。当前批次回退后持续使用主账号，直到用户再次确认启用双账号。双账号变为可用时，工作台询问是否也切换正在进行和排队的单账号批次。确认后，任务快照保存 `checkpoint.secondary_upgrade_id`。队首在途请求结算后，在写事务内按当前次账号等级重新分配符合条件批次的待发送条目，并重新计算按 UID 计的冷却。如果账号变化或次账号不可用，清除切换请求。重启时先记录未知回执，再处理已接受的切换。与队列任务的审核版本相同的预览展示批次当前分配，其他预览按当前可用账号计算。工作台在一行显示两者状态，每条弹幕独立播放发送、退出和收起动画。暂停、恢复和取消作用于整个批次，暂停或取消先停止两边领取新条目，再等待在途回执。重启时，所有在途条目分别转为未知并保留所属账号。
- 双账号批次中，一个账号没有待投递或在途条目时，worker 按接手账号当前等级从另一账号的待投递条目中分配。仅移动符合既有等级和弹幕类型规则、尚未确认且去重键未在途的条目。以两账号尚未处理完的条数尽量相等为目标，在途条目计入原账号的剩余数，奇数时优先把多出的一条交给接手账号。每次账号清空自己的条目后重新检查，分担次数不限。只剩一条待投递且没有在途条目时，仅在接手账号能更早发送的情况下转移，避免来回分配。分配变更与任务快照的 `checkpoint.share_count` 在同一事务提交，界面据此刷新每次分担后的预览。旧尝试的账号和接手账号的现有冷却期限均保留。
- 空闲账号若无法接手队首的条目，可在自己的冷却结束后领取紧随队首的排队批次中分配给同一 UID 的条目。队首暂停、取消或等待插队时停止提前领取。提前领取的批次在发送时显示运行中，回执结算后还有待投递条目则回到原队列位置。两个批次分别应用进度、回执、暂停和崩溃恢复规则。
- 按 B 站网页接口判断续期时机。成功检查缓存 30 分钟，临时故障约 1 分钟后再检查，后台运行期间定期检查。刷新和账号写入串行。先保存新 Cookie 和令牌，再确认更新，若确认中断，重启后沿用新凭据并继续确认。登录被拒或 UID 校验不符时提示重新扫码。支持旧式凭据 URL 和新版票据换 Cookie，票据只从固定 B 站主机兑换且不跟随重定向。[扫码格式实测](https://github.com/public-clis/bilibili-cli/pull/27) · [Cookie 续期记录](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/login/cookie_refresh.md)
- live 要求项目已冻结通过 B 站解析的目标分 P。支持矩阵以 bilibili-api-python 17.4.2 的请求定义为依据。live 接受普通 RGB、模式 1（滚动）、4（底部）、5（顶部）、7（高级）以及字号 18、25、36。模式 7 将源弹幕保存的 JSON 数组原样传入发送请求的 `msg`，成功回执优先使用 `dmid_str`。其他字号在冻结投递参数时改为 25。历史条目重新发送时应用当前模式和字号规则。特殊颜色、其他模式和缺少稳定来源 ID 的条目逐项记为失败。真实投递仅在人工选择跳过或已有确认投递记录时记为 `skipped`。SQLite v24 升级会重新归类旧的预检跳过记录并同步计数与任务状态。矩阵的 `platform_verified` 字段表示是否做过实际发送验收。模式和字号在平台是否可用取决于实测结果及账号权限。[SDK 17.4.2](https://pypi.org/project/bilibili-api-python/17.4.2/)
- 发送端使用既有 SDK 解析 AID，用单次 HTTPX POST 发送固定 CID、整数毫秒位置、文本和显示属性，遵循 17.4.2 的参数与 CSRF 契约。发送端关闭重定向、环境代理和隐式重试。只保存成功回包中的数字弹幕 ID，回执前缀为 `bilibili:`。HTTP 429 或平台明确限流进入持久退避并遵守有界的 `Retry-After`，按实际 UID 共享冷却。HTTP 200 回包中的业务码 36703（发送弹幕频率过快）也按限流处理，按配置限流等待（默认 180 秒）后重试当前条目，仍受账号冷却和重试次数约束。凭据拒绝或风控拒绝保留待发送条目并暂停。明确拒绝逐条记失败。超时、网络中断、无法确认的响应进入未知回执，交给人工核对。
- 发送失败且收到 HTTP 响应时，条目和对应尝试记录保存 HTTP 状态，附带已解析的 B 站错误码和脱敏消息（最多 256 字符）。诊断写入已有 JSON 字段，旧记录仍可查看本地错误码。
- 发送预览、批次创建、实际发送前和失败重试都基于已确认投递去重。自动回执确认与人工确认均在同一事务写入去重键。SQLite v19 备份旧库，从仍有明细的已确认投递重建去重表，并将未结束批次中因旧预留键误跳过的条目转回待投递。旧库中已删除明细且无法区分确认与预留的键随重建清理。
- 真实投递每次结算后按 UID 保存配置范围内的随机间隔（默认 5–7 秒），跨批次和重启共享。限流等待取随机间隔、服务端等待和配置的限流等待中的最长值。修改设置后，新计算的期限使用当前值，已保存的期限继续生效。账号连续三次明确限流或拒绝后冷却至少 60 秒，之后重新累计，确认发送也会清零。同一条第三次被限流后保持待发送并等待至少 60 秒，第四次仍限流才结束重试。明确拒绝只记该条失败，未知回执和账号或平台验证错误仍暂停。普通间隔使用 `backoff` 和 `next_attempt_at`，无错误码；延长冷却使用 `send_repeated_failures`。SQLite v7 在备份 v6 后为真实账号冷却表增加失败次数和等待原因，保留既有冷却期限。
- 发布包由 `launch-windows.cmd` 运行一个绑定 `127.0.0.1:8000` 的 Uvicorn worker，FastAPI 同源托管构建的 UI、API 和 SSE。开发模式使用 Vite 5173 同源代理并保留原始 Host。本机 UI 用无正文的 `POST /session` 自动打开会话，校验回环连接、Host、与 Host 一致的 Origin 及专用请求头。浏览器提供 `Sec-Fetch-Site` 时要求为同源。会话 Cookie 使用 HttpOnly、SameSite=Strict 和 API 路径，有效期十二小时。页面重载复用有效 Cookie，会话失效或后端重启后自动连接。其余写请求继续校验 Origin 与 CSRF token。

## 预装弹幕

- 稿件读取沿用 [BilibiliApis 的创作中心列表请求](https://github.com/cv-cat/BilibiliApis/blob/master/apis/bili_creator_apis.py) 和工作台现有的主账号 Cookie。预装候选使用定时发布状态和 `dtime`（Unix 秒）。创建项目时冻结目标 BV、分 P、CID 和片长。同一 BV 的不同分 P 可以分别建立项目。
- SQLite v28 持久化计划时间的核对结果。确认投递时冻结审核和投递账本，放行前锁定同一项目的二次提交。批次按公开时间升序放行，同一秒按项目创建顺序。
- 后端在计划公开前 10 秒验证主账号登录并从创作中心核对时间，发现改期则按新时间重新排程。查询失败或登录不可用时保留批次，间隔至少 5 秒再试。公开时间后满 5 秒重新检查主账号登录并放行原批次。
- 预装批次优先于普通批次。正在发送的普通批次在既有的投递结算边界让出队首。人工插队通过队首任务 ID 确认覆盖预装优先级。预装批次首条确认受阻或被拒绝时，间隔 1 分钟最多重试两次。两次仍明确失败时撤回批次，项目回到“弹幕待发送”。限流沿用既有冷却规则，回执未知时等待人工核对。
- 后端启动时发现离线期间到期的计划会等待一次队列决定。已处理到期事件的任务按持久化时点继续。前端在主账号登录有效后呈现过期确认。选择放弃时撤销尚未投递的批次并保留审核和发送记录。
- `Project.preload_publish_at` 是当前计划的 Unix 秒值，`publication_status` 为 `waiting`、`checking`、`blocked` 或 `null`。发送检查点的 `publication` 字段分别用 `pending`、`dispatch` 和 `returned` 标记等待放行、进入投递队列和退回待发送。`publication_first_pending` 和 `publication_retry_at` 保存首条投递的状态和下一次非限流重试时间。

## 部署与安全

SQLite 启用外键、WAL、busy_timeout，索引项目/时间、任务状态/下次执行时间和事件序号。私有数据根可配置。首次引入表即记录 schema_version，升级前备份数据库，按版本事务迁移，阻止未知新版本启动。

会话、SSE、凭据和项目均校验主体，项目所有者可编辑。Windows 密钥保存到系统凭据库。配置摘要可读，密钥只写。禁止记录 Cookie、URL 密钥、请求和响应原文及校验输入。模型端点与密钥绑定，更换主机须重新授权。B 站凭据只交给 B 站适配器。

未来 Linux Docker 将 UI 构建物与 Python 服务打包，持久卷挂载数据库和字幕。受信反向代理终止 HTTPS、透传 SSE、限制上传，Cookie 启用 Secure。远程部署使用账户或 OIDC 会话，保持相同 Principal 与项目授权契约。多 worker 或多实例前先实现跨进程租约和投递互斥，再评估服务端数据库。

### 真实投递失败重试

- 后端每秒向 B 站 API 发送无凭据的 HTTPS HEAD 探测，单次最多等待一秒。收到 HTTP 响应即视为网络可达，平台限流和账号错误由投递逻辑单独处理。探测失败或发送发生传输错误时，队首停止发起新请求，已发出的请求照常结算。探测成功后按队列顺序继续，保留手动暂停与原有冷却期限。
- 真实批次首次处理完有失败时，事务内仅将失败条目重新排队一次，并持久化 `checkpoint.automatic_retry_started`。重试复查预检与已确认记录，重新计算限流预算并遵守账号冷却。第二轮结束后，剩余失败使任务以 `send_partial` 结束并释放队首。未知回执仍等待逐条人工核对。历史已停在 `send_failed_review` 的批次仍可通过既有人工决定接口处理。
- 发送尝试结算时在同一事务中保存本次实际采用的常规和限流等待，用于账号级速率统计。额外的连续失败冷却、网络请求耗时及人工暂停不计入。旧尝试优先根据任务事件中的冷却截止时间恢复等待时长。没有可用截止时间时，按当前常规等待区间的中点及限流等待设置估算，最多计入本次尝试结束至下一次尝试开始的时长。统计跨服务重启。
- `POST /tasks/{task_id}/deliveries/retry-failed` 接收 `{expected_version}`，重新排队明确失败的条目，保留原批次、回执和尝试历史。等待决定的批次保留队首，历史已结束批次重新入队。同键弹幕已有确认投递时，该失败条目标为跳过。每次人工重试重新计算单条限流预算，沿用原目标、账号校验和账号冷却。
- `POST /tasks/{task_id}/deliveries/continue-queue` 接收 `{expected_version}`，将等待决定的批次结束为 `failed`，保留失败明细并释放队首。两个写接口均校验项目所有者、CSRF 和任务版本，返回更新后的任务。版本冲突或当前状态不可操作时返回 409。等待决定的批次通过这两个接口处理，常规 `resume` 返回 409。未知回执继续使用逐条核对流程。

验证使用合成弹幕与模拟发送器。后端 83 项测试通过，前端 19 项测试通过，生产构建通过。

### 项目迁移完成标记

- 创建、列表和详情返回布尔字段 `completed`。自动判定要求项目至少有一个未取消的真实投递任务，且全部参与判定的真实投递均已处理完毕、无失败和未知结果。模拟投递、已取消批次及下载和分析任务无需参与判定。失败重试以原任务的当前进度为准。历史项目也从现有任务数据自动计算。
- `Project` 返回 `activity: {stage, running, active}`。`stage` 为 `download`、`classify`、`review` 或 `send`。真实投递批次首次被 worker 领取时会持久化 `checkpoint.started`。已开始、仍未结束且仍有未成功或未跳过弹幕的批次使项目卡片显示“弹幕发送中”，包括暂停、限流退避和恢复后的排队。批次成功或失败结束后退出发送状态。所有真实投递批次中的弹幕均成功或跳过时，项目自动完成。发送中的批次和正在执行或正在暂停的下载、分析任务使 `running` 为 `true`，并优先于等待任务。组内按发送、分析、下载顺序显示。项目列表定期刷新此状态，当前项目的卡片也随任务事件更新。已完成项目在 `running` 和 `active` 同时为 `true` 时显示任务状态，其余时间显示绿色对勾。
- 无上述任务时，最近的发送或成功分析任务决定待处理阶段。最近的是成功分析时，卡片显示“弹幕待审核”（`review`），点击“前往预览&发送”并成功保存当前审核修订后显示“弹幕待发送”（`send`）。修改审核选择使原确认失效。已有有效审核确认或最近的是发送任务时显示“弹幕待发送”。已有分析或下载成功的其余项目待分析，其余项目待下载。
- `Project.send_queue` 返回项目最靠前的等待批次在全局等待队列中的 `{position, total}`，无此类批次时为 `null`。位置从 1 开始。排名统计 `queued` 批次，队首已开始投递时按发送中的批次处理，包括重新领取前短暂回到 `queued` 的情况。项目卡片处于待发送阶段且有排队批次时显示“投递排队中（位置/总数）”，排名随项目列表刷新。
- `Project.send_needs_review` 表示项目有真实投递批次已处理完待投递条目，且仍有未知回执。未完成的项目以警示色显示“需人工核对”，即使同一项目还有其他批次排队。自动完成要求所有未知回执均已核对。`Project.send_partial_failure` 表示项目有已处理完待投递条目且仍有失败明细的真实批次，未完成的项目卡片以琥珀色问号显示「部分弹幕发送失败，待核对」。完成对勾优先于卡片上的投递警示，投递明细保留原始结果。
- `PATCH /projects/{project_id}/completion` 接收 `{"completed": true}` 或 `{"completed": false}` 并返回更新后的项目。项目卡片菜单在用户确认后提交。接口校验项目所有者与 CSRF，找不到项目返回 404，布尔值格式错误返回 422。排队、退避、运行、正在暂停或正在取消时，`Project.activity.active` 为 true，「标记为已完成」返回 409 `project_tasks_active`。完全暂停或已结束的任务允许标记为已完成，部分失败或待核对的投递也适用。「标记为未完成」随时可用。数据库繁忙返回可重试的 503。手动设置覆盖自动判定，随项目删除。
- SQLite v8 新增 `project_completion_overrides` 表，升级前备份旧数据库。

后端 88 项测试通过，涵盖旧库升级、成功、失败重试和手动覆盖。前端 22 项测试和生产构建通过。Chrome 验证了绿色对勾、两种确认操作、取消和刷新后保留状态。

### 转载公告 UP 主弹幕

- B 站源快照完整下载后，按弹幕原文检查是否已有“站内转载，因此会将之前的弹幕一并搬运过来（不含弹幕翻译）”。缺少时为该快照保存一条审核候选，位置为 500 毫秒，使用白色、25 号字、顶部模式 5 和 `checkbox_type=4`。候选会出现在人工审核列表中并默认勾选，用户可按运行取消勾选。快照的下载计数和模型分类输入只包含下载的条目。
- 预览与投递批次冻结 `checkbox_type`。普通条目和旧投递记录的默认值为 0。真实发送为公告提交 `checkbox_type=4`，并沿用项目、目标 CID 和稳定条目键的已确认投递去重与重试规则。下载检查使用源快照，投递去重使用本地账本。
- SQLite 升级至 v9 时备份原库，并为已有完整 B 站快照补建缺失的审核候选。新快照的生成与最后一个下载分段在同一事务提交，重复处理同一快照只保留一条候选。

后端 93 项测试和前端 22 项测试通过，生产构建通过。发送表单使用模拟 HTTP 验证。
