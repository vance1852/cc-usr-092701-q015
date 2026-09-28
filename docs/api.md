# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 质量委员会聚合分析

质量分析只面向 `quality_officer`（仅 `quality:read`）与负责人（另有 `quality:manage`）。质量岗位不能读取任何患者级接口；所有结果都是项目类别 × 时间段的去标识汇总，不返回患者编号、医生编号或明细记录。

- `GET /quality/rules` 返回纳入规则、空值原因、抑制原因与排除原因的代码说明。
- `GET /quality/config` 查看当前抑制门槛；`POST /quality/config`（仅负责人）设置 `min_cell_count`、`min_cell_delta` 和 `followup_window_days`。最小单元人数必须至少为最小互补差的两倍。
- `GET /quality/aggregates?granularity=month&start=2026-08-01&end=2026-08-31&category=weight&category=aesthetic` 按对齐的日/周/月/季度时间段计算。起止日期必须分别落在完整时间段边界上，一次最多 12 个月（周日 13 个、季度 4 个、日 31 个）。
- `POST /quality/exports` 参数同上（类别字段为 `categories`），冻结导出已结束且随访宽限期已过的时间段；未结束或宽限期未结束的时间段不能冻结。

每格返回 `headcount`（去重人数）、`appointments`（scheduled/fulfilled/no_show/cancelled 与履约率）和 `followups`（due/completed/deferred 与完成率），并随附 `window`（按诊所时区解释的 UTC 界）、`inclusion_rules` 与 `data_snapshot`（审计链序号、链头摘要与快照版本）。

隐私保护规则：

- 单元去重人数低于 `min_cell_count` 时整格抑制（`status: "suppressed"`），不返回任何计数，无法用两个类别或两种筛选相减还原小样本。
- 指标的任一非零组成部分本身或其互补小于 `min_cell_delta` 时，该指标整组抑制（`suppressions[].reason = "complement_small"`），不发布履约/完成及其分量，防止用相邻筛选条件相减定位个体。
- 时间段必须与日/周/月/季度对齐，不能通过平移一天等相邻窗口差分还原被抑制数量。
- 占位预约（held）与已取消随访不计入分母；无计划、无法归因到类别的预约与随访在 `exclusions` 中说明，计数同样经过小数量门控。
- 分母为零、随访宽限期未结束、时间段未结束分别给出 `nulls[].reason`（`no_scheduled_appointments`、`no_due_followups`、`followup_grace_open`、`period_not_closed`），空值与抑制含义不同。

队列要求患者持有评估时仍有效的 `quality_aggregate` 授权；撤回授权后患者不进入此后的任何快照，重新签署更高版本授权后才可再次纳入。从未进入生效状态的计划不队列，已合并或关闭档案不队列。

冻结导出把时间窗口、纳入规则、配置指纹和数据快照版本一并固化。之后的迟到更正、补录、授权撤回或门槛调整都不改变已导出结果；重复导出同一周期返回相同 `export_id` 且 `replayed: true`，不同分院各自复算同一周期、互不可见。规则变更后读取旧导出时附加 `advisories` 说明该格形成于另一套配置。所有配置变更和导出进入审计哈希链。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
