# Atlas 前端与 SSE 流式交互规范

状态：Active
版本：2026.08
Owner：Agent Experience

## 状态模型

前端将一次消息提交建模为 `submitting -> running -> terminal`，终态为 completed、failed 或 cancelled。
页面展示服务端公开事件，不根据计时器伪造“正在检索”。每个事件包含单调 `sequence`、`run_id`、类型、
安全文案和时间。

## 重连

浏览器断线后使用最后 sequence 重连；服务端重放缺失事件。重复事件按 `(run_id, sequence)` 去重，乱序
事件先缓冲再合并。切换会话时旧请求可以继续，但其响应不得覆盖当前会话；React effect cleanup 只取消
本地订阅，不调用服务端 cancel。

## 用户反馈

300 ms 内展示已接收状态；10 秒无新阶段显示“仍在处理”，30 秒说明可离开后回来。工具事件显示动作、
状态、耗时和结果数量，不展示内部 Prompt、模型思维过程或数据库 payload。引用在回答完成前可增量显示，
最终以服务端 Answer contract 为准。

## 可访问性与移动端

输入框、发送、取消和附件状态必须键盘可达；动态状态使用适度 `aria-live`，避免每个 token 都朗读。固定
底部导航与 composer 之间保留安全区，390 px 宽度不得横向滚动。工具详情折叠不应改变主输入区高度。

## 错误恢复

认证失效、provider busy、timeout、上传失败和 run interrupted 使用不同稳定文案。失败消息保留用户输入
并提供原位重试；刷新后从 conversation/run API 恢复，不依赖仅存在内存中的前端状态。
