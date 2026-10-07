你需要设计并实现一个“釜山大学公告智能抓取、AI 分析与邮件订阅系统”。

当前阶段只支持三个官方公告源，不要擅自扩展其他来源。

## 一、项目目标

系统面向釜山大学学生。

用户不需要安装任何 App，也不需要配置第三方通知工具。

用户只需要：

访问网页
→ 输入邮箱
→ 验证邮箱
→ 选择订阅内容
→ 后续通过邮箱持续接收学校公告

完整流程：

学校公告
→ 自动抓取
→ 保存原始内容
→ 检测新公告 / 更新公告
→ AI 分析
→ 提取重要信息
→ 判断受众与重要程度
→ 根据用户订阅偏好匹配
→ Resend 邮件发送
→ 截止日期提醒
→ Daily / Weekly Digest

系统必须优先保证：

1. 不漏公告
2. 不重复通知
3. 不把旧公告误认为新公告
4. AI 不得编造原文不存在的信息
5. 所有 AI 分析都必须能够回到学校原始公告核实
6. 历史数据和实时新增数据必须严格区分
7. 用户可以随时修改订阅或退订
8. 未验证邮箱不得正式进入邮件分发系统


# 二、第一阶段数据源

目前只支持以下三个来源。

### Source 1：计算机本科生公告

名称：

PNU CSE Undergraduate Notices

公告页面：

https://cse.pusan.ac.kr/cse/14221/subview.do

RSS：

https://cse.pusan.ac.kr/bbs/cse/2055/rssList.do?row=50

source_key：

cse_undergraduate

默认受众：

- 釜山大学计算机相关本科生

网页中可能包含的分类包括：

- 일반
- 수업
- 장학
- 면담
- 이전자료

注意：

这个公告板发布数量较大，RSS 最近 50 条不能保证覆盖历史回填区间。


### Source 2：计算机大学院公告

名称：

PNU CSE Graduate Notices

公告页面：

https://cse.pusan.ac.kr/cse/14227/subview.do

RSS：

https://cse.pusan.ac.kr/bbs/cse/2058/rssList.do?row=50

source_key：

cse_graduate

默认受众：

- 计算机专业硕士生
- 计算机专业博士生
- 수료후 연구생
- 其他大学院相关学生


### Source 3：国际处留学生公告

名称：

PNU International Student Notices

公告页面：

https://international.pusan.ac.kr/international/15224/subview.do

RSS：

https://international.pusan.ac.kr/bbs/international/2081/rssList.do?row=50

source_key：

international_student

默认受众：

- 釜山大学外国留学生

网页本身包含的主要分类包括：

- 비자
- 학사
- 기숙사
- 장학
- 보험
- 취업
- 교내활동
- 대외활동
- 홍보
- 정보


# 三、历史数据初始化

系统第一次运行时，需要进行 Historical Backfill。

历史数据起点固定为：

2026-07-01 00:00:00 Asia/Seoul

目标：

导入从 2026-07-01 至系统首次运行时间之间的所有公告。

不要假设 RSS 的 50 条记录足够。

历史回填流程：

1. 首先读取 RSS。
2. 判断 RSS 是否已经覆盖到 2026-07-01。
3. 如果 RSS 最旧公告仍晚于 2026-07-01：
   - 使用公告网页分页继续向后抓取。
4. 持续翻页，直到确定已经进入 2026-07-01 以前的普通公告。
5. 只保存 2026-07-01 及以后发布的公告。
6. 注意顶部固定公告 / 공지 可能发布日期非常早，不能因为单条旧固定公告就停止翻页。
7. 停止条件必须根据普通分页内容的整体日期范围判断。

首次历史回填过程中：

- 保存公告
- 抓取详情
- 执行 AI 分析
- 建立 deadline
- 建立分类

但是：

禁止向现有用户逐条发送历史公告。

历史公告必须：

historical_import = true

首次初始化完成后，可以生成一次历史数据统计，但不能批量发送历史邮件。


# 四、实时抓取机制

历史回填完成后进入 Incremental Monitoring。

默认：

每 15 分钟检查一次三个 RSS。

时间间隔必须配置化：

POLL_INTERVAL_MINUTES=15

每次执行：

RSS
→ Parse
→ Normalize
→ Identify
→ Compare DB
→ Fetch detail if needed
→ Store
→ AI Analysis
→ Subscriber Matching
→ Email Queue

RSS 是发现机制。

公告详情页才是事实来源。

不能只保存 RSS title 和 description。


# 五、公告唯一标识

必须设计可靠去重机制。

优先从以下内容提取：

- RSS GUID
- 公告 URL 中稳定公告 ID
- nttId
- board ID
- source_key

推荐唯一键：

source_key + external_notice_id

URL 作为辅助。

禁止仅通过 title 判断是否重复。


# 六、公告数据模型

每个 notice 至少保存：

id

source_key

external_notice_id

original_title

original_url

author

published_at

category_original

raw_html

clean_text

created_at

last_checked_at

historical_import

content_hash

ai_status

processing_status


附件：

attachments[]

每个附件至少保存：

- filename
- url
- extension


# 七、公告内容抓取

RSS 发现公告后，访问对应详情页。

提取：

1. 标题
2. 作者
3. 发布时间
4. 正文
5. 分类
6. 附件列表
7. 外部链接
8. 公告 ID

HTML 清理后生成：

clean_text

同时必须保留：

raw_html

不能只保存 clean_text。

以后解析逻辑升级时，应允许重新分析旧数据。


# 八、附件处理

第一阶段至少保存：

- 文件名
- 下载 URL
- 文件类型

如果附件格式可以安全解析，例如：

PDF
DOCX
XLSX
TXT

可以提取文本后加入 AI context。

如果暂时不能可靠解析：

HWP
HWPX
扫描图片
特殊格式

不要猜测附件内容。

AI 输出中应该允许：

attachment_requires_review = true

如果重要要求可能存在附件内，邮件中提示：

“该公告包含附件，自动摘要可能未覆盖附件内全部要求，请查看学校原附件。”


# 九、内容变更检测

保存：

content_hash

建议基于标准化后的：

标题 + 正文 + 附件列表

计算。

如果：

external_notice_id 已存在

并且：

content_hash 未变化

则：

不产生新通知。

如果：

external_notice_id 已存在

但：

content_hash 改变

则认为：

NOTICE_UPDATED

保存 revision。

notice_revisions 至少记录：

notice_id
old_hash
new_hash
detected_at

重要更新可以发送：

[公告更新]

不能重新当作：

[新公告]


# 十、AI 分析层

抓取和保存必须独立于 AI。

正确流程：

Crawler
→ Database
→ AI Queue
→ AI API

即使 AI API 暂时失败：

公告仍然必须成功保存。

AI 失败不能造成数据丢失。


# 十一、AI 任务

AI 不负责决定“原文是什么”。

AI 只负责理解已经抓取并保存的官方公告。

每篇公告分析以下内容：

### 1. 中文标题

title_zh

准确翻译，不进行营销化改写。


### 2. 中文摘要

summary_zh

约 100～300 中文字。

至少说明：

- 这是什么
- 谁需要关注
- 最重要的事项


### 3. 公告分类

categories：

academic
course
graduation
thesis
scholarship
visa
immigration
employment
dormitory
insurance
research
competition
event
administration
international
safety
other

允许多标签。


### 4. 受众分析

audience：

undergraduate
master
phd
graduate
international_student
prospective_student
graduating_student
chinese_student
specific_nationality
all_students

不要仅根据来源推断。

必须结合正文。


### 5. 是否需要行动

action_required

boolean


### 6. 用户需要做什么

actions[]

例如：

- 在线申请
- 提交材料
- 访问办公室
- 注册
- 邮件提交
- 问卷填写
- 无需操作


### 7. 截止日期

deadlines[]

每个 deadline：

{
  "type": "",
  "datetime": "",
  "timezone": "Asia/Seoul",
  "original_text": "",
  "confidence": 0.0
}

必须区分：

公告发布日期

和

实际截止日期。

禁止把公告发布日期误识别为 deadline。


### 8. 资格要求

eligibility[]

只能从原文提取。

例如：

- 仅本科生
- 博士毕业预定者
- 中国籍学生
- TOPIK 4级以上
- 2027年2月毕业预定者


### 9. 所需材料

required_documents[]

原文没有明确说明时：

返回空数组。

不能自行补充常识。


### 10. 重要度

importance：

critical
high
normal
low

基本逻辑：

critical：
签证身份、强制行政手续、极短截止时间、重大紧急事项。

high：
毕业、论文、选课、奖学金、注册、重要资格申请。

normal：
一般招聘、活动、讲座、普通行政通知。

low：
宣传、非必要活动、泛信息。


### 11. 推荐通知方式

delivery_priority：

immediate
daily
weekly

这是 AI 推荐值。

最终是否发送仍需结合：

用户订阅偏好 + 系统规则。


### 12. 风险提示

warnings[]

例如：

- 截止日期很近
- 仅适用于博士生
- 已经过期
- 附件可能包含重要要求
- 资格条件复杂


### 13. 状态

status：

active
upcoming
expired
closed
informational


# 十二、AI 强制规则

AI 必须遵守：

1. 所有结论只能来自提供的公告正文和附件文本。
2. 不允许基于韩国大学常识补全。
3. 不允许推测签证政策。
4. 不允许推测毕业条件。
5. 不允许推测奖学金资格。
6. 无法确定时使用 null、[] 或 unknown。
7. 日期必须保留原始文字。
8. AI 输出不是官方信息。
9. 原始公告 URL 必须永久保留。
10. 如果标题或正文明确出现：
   - 마감
   - 종료
   - Closed

必须判断是否已经结束。


# 十三、AI 返回格式

AI 必须强制返回结构化 JSON。

示意：

{
  "title_zh": "",
  "summary_zh": "",
  "categories": [],
  "audience": [],
  "action_required": false,
  "actions": [],
  "deadlines": [],
  "eligibility": [],
  "required_documents": [],
  "importance": "normal",
  "delivery_priority": "daily",
  "warnings": [],
  "status": "active",
  "attachment_requires_review": false,
  "confidence": 0.0
}

API 层必须执行 JSON Schema validation。

非法 JSON：

自动重试。

仍失败：

ai_status = failed

但不能影响公告本身保存。


# 十四、用户注册方式

用户通过网站注册订阅。

不提供用户名密码体系。

第一阶段采用：

Email Subscription

用户访问订阅页面：

/subscribe

输入邮箱地址。

例如：

student@example.com

提交后：

不能立即激活订阅。

必须进行邮箱验证。


# 十五、邮箱验证流程

推荐使用 Double Opt-In。

流程：

用户输入邮箱
→ 创建 pending subscriber
→ 生成一次性 verification token
→ Resend 发送验证邮件
→ 用户点击验证链接
→ 邮箱状态变为 verified
→ 才正式接收公告

verification link 示例：

https://YOUR_DOMAIN/verify?token=xxxxx

token 必须：

- 随机
- 不可预测
- 有过期时间
- 只能使用一次

例如：

24 小时后失效。


# 十六、首次订阅偏好

首次注册可以尽量保持简单。

最少提供三个来源：

☑ 计算机本科生公告

☑ 计算机大学院公告

☑ 国际处留学生公告

用户至少选择一个。

同时可以选择通知频率：

☑ 重要公告立即通知

☑ 每日汇总

☑ 每周汇总

默认推荐：

重要公告立即发送

普通公告每日汇总

低优先级公告每周汇总


# 十七、无密码订阅管理

第一阶段不要为了管理订阅引入完整账户系统。

使用：

Manage Subscription Link

每封邮件底部包含：

管理我的订阅

点击后进入：

https://YOUR_DOMAIN/subscription/manage?token=xxxxx

token 与 subscriber 绑定。

用户可以：

- 修改订阅来源
- 修改通知频率
- 暂停订阅
- 重新启用
- 退订

不需要输入密码。

管理 token 必须足够随机并可撤销。


# 十八、退订

每封营销/订阅性质邮件必须包含清晰退订入口。

例如：

取消订阅

点击后：

unsubscribe subscriber

或者：

只关闭某类来源。

系统需要保存：

unsubscribed_at

不要删除 subscriber 历史记录。

避免用户退订后因为历史任务再次被加入发送队列。


# 十九、Subscriber 数据模型

subscribers 至少保存：

id

email

email_normalized

status

created_at

verified_at

unsubscribed_at

language

verification_token_hash

verification_expires_at

management_token_hash


status：

pending

active

paused

unsubscribed


# 二十、Subscription 数据模型

subscriptions 至少保存：

id

subscriber_id

source_key

enabled

immediate_enabled

daily_digest_enabled

weekly_digest_enabled

created_at

updated_at


未来可以扩展：

category preferences

audience preferences

但第一阶段不需要过度复杂。


# 二十一、邮件发送渠道

所有邮件统一通过：

Resend

使用：

RESEND_API_KEY

API Key 不允许写入 repository。

使用环境变量。

推荐：

FROM_NAME="PNU Notice"

FROM_EMAIL="notice@YOUR_DOMAIN"


# 二十二、新公告发送逻辑

发现新公告后：

抓取详情
→ 保存
→ AI 分析
→ 判断 importance
→ 获取所有订阅该 source 的 active subscriber
→ 根据用户通知偏好匹配
→ 创建 delivery jobs

例如：

critical / high

且用户开启 immediate：

立即发送。

normal：

进入 Daily Digest。

low：

进入 Weekly Digest。


# 二十三、即时邮件

Immediate Email 推荐格式：

------------------------------------------------

PNU Notice

【重要度：高】

硕士学位论文申请通知

来源：
计算机大学院

发布时间：
2026-09-17

截止：
2026-10-07

适合人群：
2027年2月预计毕业的硕士生

中文摘要：

……

你需要做：

1. ……
2. ……
3. ……

需要准备：

- ……
- ……

注意：

……

查看学校原公告

------------------------------------------------

固定免责声明：

“以上内容由 AI 根据釜山大学官方公告自动整理，仅用于辅助快速理解。具体要求请以学校原公告及附件为准。”

邮件底部：

管理订阅

取消订阅


# 二十四、Daily Digest

普通公告不应逐条轰炸用户。

默认：

每天 20:00 Asia/Seoul

生成：

今日 PNU 公告摘要

例如：

今天共有 4 条与你订阅内容相关的公告。

1.
【奖学金】
国际学生奖学金申请

2.
【就业】
招聘说明会

3.
【大学院】
论文提交日程调整

每条包含：

- 中文标题
- 来源
- 简短摘要
- 是否需要行动
- deadline
- 原文链接


# 二十五、Weekly Digest

默认：

每周日 20:00 Asia/Seoul

主要用于：

- 活动
- 宣传
- 讲座
- 低优先级信息
- 一周汇总


# 二十六、Deadline Reminder

AI 提取出明确 deadline 后：

创建 deadline entity。

默认：

D-7
D-3
D-1
D-Day

但必须避免补发轰炸。

例如：

首次发现公告时：

deadline 只剩 2 天

则只建立：

D-1
D-Day

已经过期：

不创建 future reminder。


# 二十七、Deadline Reminder 订阅匹配

Reminder 只能发送给：

1. 当前仍为 active 的 subscriber
2. 当前仍订阅该 source
3. 没有关闭 immediate/deadline 类通知
4. 没有退订

不能因为用户曾经订阅过就继续发送。


# 二十八、邮件去重

必须建立：

notification_deliveries

至少保存：

id

subscriber_id

notice_id

channel

notification_type

scheduled_at

sent_at

status

provider_message_id

error

唯一性建议：

subscriber_id
+
notice_id
+
notification_type

防止任务重跑造成重复发送。


# 二十九、通知类型

notification_type：

new_notice

updated_notice

deadline_d7

deadline_d3

deadline_d1

deadline_day

daily_digest

weekly_digest

verification

subscription_management


# 三十、Resend 失败处理

邮件发送失败不能丢失任务。

状态：

pending

sending

sent

failed

retrying

永久失败时：

记录错误。

对于：

invalid email
hard bounce

应考虑将 subscriber 标记：

email_invalid

不要无限重试。


# 三十一、抓取任务运行记录

建立：

crawl_runs

至少保存：

source_key

started_at

finished_at

status

items_seen

new_items

updated_items

error_message

这样以后出现：

“今天为什么没有邮件？”

可以判断：

是真的没有公告

还是 crawler 失败。


# 三十二、异常监控

以下情况必须记录 error：

RSS 无法访问

网页 HTTP 状态异常

DOM 结构变化

详情页解析失败

AI API 失败

Resend 失败

数据库失败

Digest job 失败

Deadline scheduler 失败

连续多次失败：

向管理员发送邮件告警。

管理员告警同样可以通过 Resend。


# 三十三、抓取请求原则

这是学校官方网站。

必须温和抓取。

User-Agent：

PNU-Notice-Monitor/1.0

RSS：

可以正常周期检查。

历史回填：

建议控制在：

1～2 requests / second 以下。

遇到：

429
5xx

使用 exponential backoff。

禁止高并发扫描学校网站。


# 三十四、时间处理

全系统业务时间统一：

Asia/Seoul

数据库可以存 UTC。

所有：

发布日期
截止日期
Digest 时间
Reminder 时间

统一按：

Asia/Seoul

计算。


# 三十五、建议数据库结构

至少考虑：

sources

notices

notice_revisions

attachments

ai_analyses

deadlines

subscribers

subscriptions

email_verifications

notification_deliveries

crawl_runs


# 三十六、系统处理状态

notice 可以支持：

discovered

fetched

stored

ai_pending

ai_completed

ai_failed

delivery_pending

processed

不要把：

抓取
AI
发送

塞在同一个不可恢复的大函数中。


# 三十七、幂等性

整个 pipeline 必须是 idempotent。

同一 RSS 执行 10 次：

不能产生 10 条公告。

同一 notice AI 重跑：

不能产生重复邮件。

同一邮件 job 重跑：

不能重复发送。

同一 verification link：

不能多次激活。


# 三十八、首次运行流程

第一次部署：

START

↓

初始化数据库

↓

初始化三个 sources

↓

Historical Backfill

↓

抓取 2026-07-01 至今

↓

保存详情

↓

去重

↓

AI 分析历史公告

↓

建立 deadline

↓

historical_import=true

↓

禁止历史单条邮件

↓

记录 backfill checkpoint

↓

切换 realtime monitoring

↓

每 15 分钟 RSS polling

↓

发现新公告

↓

保存原文

↓

AI 分析

↓

匹配 active subscribers

↓

Immediate
或
Daily Digest
或
Weekly Digest

↓

如果存在 deadline

↓

建立未来 Reminder


# 三十九、用户使用流程

用户：

打开网站

↓

输入邮箱

↓

选择：

计算机本科公告

计算机大学院公告

国际处公告

↓

选择通知频率

↓

提交

↓

收到验证邮件

↓

点击 Verify

↓

订阅成功

↓

以后自动收到邮件


# 四十、用户无需理解系统技术结构

前端不要出现：

RSS

Crawler

AI Queue

Resend

Source Key

Webhook

等技术术语。

普通用户只需要理解：

“选择你关心的釜山大学公告，我们会在有重要更新时发送到你的邮箱。”


# 四十一、前端第一阶段

只需要三个公开页面。

### /

介绍服务：

PNU Notice

自动整理釜山大学重要公告，并通过邮箱通知你。


### /subscribe

邮箱输入

三个公告来源选择

通知频率选择

订阅按钮


### /subscription/manage

通过 token 访问。

可以：

修改来源

修改频率

暂停订阅

取消订阅

第一阶段不需要：

用户头像

用户名

密码

复杂 Dashboard


# 四十二、隐私原则

只收集提供服务所必要的数据。

第一阶段尽量只保存：

Email

订阅偏好

发送记录

不要因为“以后可能需要”就收集：

姓名
学号
手机号
生日
学院
国籍

如果未来个性化需求真的需要，再增加。


# 四十三、开发优先级

严格按照以下阶段开发。

## Phase 1：Crawler

完成：

三个 RSS

详情页抓取

历史回填

数据库

去重

content hash


## Phase 2：AI

完成：

AI structured output

中文摘要

分类

audience

deadline

importance


## Phase 3：Email Subscription

完成：

网页提交邮箱

邮箱验证

Subscriber

Subscription

管理订阅

退订


## Phase 4：Email Delivery

接入：

Resend

Immediate Email

delivery logging


## Phase 5：Scheduler

加入：

RSS polling

Daily Digest

Weekly Digest

Deadline Reminder


# 四十四、最重要的系统边界

不要：

因为 AI API 不可用就停止