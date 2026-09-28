# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

岗位包括负责人、临床医生、护理、运营协调员、内审员与质量专员（`quality_officer`）。质量专员只持有 `quality:read`，不能读取任何患者级记录、日常运营报告或审计内容。

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

## 质量委员会汇总快照

`POST /reports/quality-snapshots` 供质量专员与负责人比较医美、体重管理项目的完成率、预约履约与随访完成。接口只返回项目类别（`aesthetic`/`weight`）与对齐自然周期（`monthly` 传 `month`、`quarterly` 传 `quarter`、`yearly`，均含 `year`）的去重汇总，不接受医生、患者、分院以外筛选或滑动时间窗口，因此无法通过相邻筛选相减还原小单元。请求可携带 5 至 50 之间的 `threshold`（默认 5）。

- **小单元抑制**：类别 × 周期单元去重人数为 1 至门槛-1 时整单元抑制为 `null`；空单元标记为 `empty`。
- **互补抑制**：周期小计、类别小计、总计与其已发布子单元之差（残差）为 1 至门槛-1 时整体抑制；事件预约量/到诊量、随访到期量/完成量及其互补差同样校验，必要时只发布总量、抑制完成量与比率。报表内不提供候选总体总数，避免排除计数之间互减。
- **纳入规则**：快照生成时点（`data_cutoff_at`）患者须在诊，且持有有效的 `quality_aggregation` 用途授权；从未签署、已撤回或已过期的患者不纳入，并在 `exclusions` 中按规则代码（未在诊、无授权、已撤回、已过期）分别说明；排除计数本身低于门槛也会抑制。预约分母排除 `held` 占位，到诊及以后计为履约，未到诊/取消计为未履约；随访分母排除已取消任务。无计划关联的事件不归入任何类别。
- **不可变快照**：同分院、同周期网格、同门槛与规则版本只生成一份快照并持久化结果；之后的迟到更正、补录或授权撤回都不改变已导出内容（重放返回 `replayed: true` 与同一 `snapshot_id`、`source_digest`）。撤回 `quality_aggregation` 授权的患者仅在之后 cutoff 的新快照中消失。不同分院对同一周期各自独立生成与留痕。
- **留痕字段**：每份快照记录 `window`（诊所时区解释的周期起止与 UTC 边界）、`inclusion_rules.rules_version` 与逐条规则、`format_version`、`service_version`、`data_cutoff_at`、`source_digest` 与门槛；`cells`、`period_totals`、`category_totals`、`grand_total` 中每个数值都带 `status` 与 `reasons` 规则代码，可区分空值、小单元抑制、互补抑制与小计数抑制。生成、重放与查看均写入不可变审计事件，结果体不含患者或员工标识。

`GET /reports/quality-snapshots` 按 `granularity`、`year` 列出本分院快照元数据；`GET /reports/quality-snapshots/{snapshot_id}` 取回指定快照。跨分院访问返回不存在。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
