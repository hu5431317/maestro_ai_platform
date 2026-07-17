# 自然语言 APP 测试用例模板

> 用途：用最少信息描述 APP 测试需求，AI 会根据本模板和 Skill 自动生成可执行 Maestro YAML。
> 说明：只需要填写业务意图，不需要写 Maestro 语法；复杂规则、AI 断言、自愈、标签、数据驱动和跨平台兼容由 Skill 处理。

---

## 默认配置

```yaml
app_id: "@flyu"          # 必填：应用别名，见 config/apps.yaml
platform: "android"      # 必填：android / ios / both
device: "oppo A32"       # 可选：设备别名，见 config/devices.yaml
account: "default"       # 可选：default 表示使用 Skill/环境变量中的默认测试账号
tags: ["regression"]     # 可选：默认标签，可在单个用例中覆盖
```

---

## 填写规则

每条用例只需要填写 5 个核心字段：

```yaml
- id: "CASE_001"          # 必填：唯一编号
  title: "用例标题"        # 必填：一句话说明测什么
  tags: ["smoke"]         # 可选：smoke / regression / login / home / mv_studio / settings / create
  precondition: "前置条件" # 可选：例如 已登录、未登录、网络正常
  steps:                  # 必填：按顺序写自然语言步骤
    - "第 1 步"
    - "第 2 步"
  expect: "预期结果"       # 必填：最终要验证什么
```

### 填写建议

- 一个步骤只写一个动作。
- 账号、密码、设备、环境等可变数据不要写死，除非临时调试需要。
- 如果需要登录，直接写“先登录”即可，AI 会优先复用登录子流程。
- 如果页面验证比较复杂，直接用自然语言描述预期，AI 会生成 `assertWithAI`。
- 如果不确定标签，可以不填，AI 会按标题和步骤自动补充。

---

## 用例列表

```yaml
cases:
  - id: "LOGIN_001"
    title: "邮箱账号登录成功"
    tags: ["smoke", "regression", "login"]
    precondition: "未登录，网络正常"
    steps:
      - "打开 App"
      - "点击邮箱登录入口"
      - "输入默认测试账号"
      - "输入默认测试密码"
      - "点击 Login 按钮"
    expect: "登录成功并进入首页，不能停留在登录弹窗或登录入口页"

  - id: "MV_001"
    title: "MV Studio 页面展示四个 Tab"
    tags: ["smoke", "regression", "mv_studio"]
    precondition: "已登录，网络正常"
    steps:
      - "点击底部 MV Studio Tab"
      - "查看页面顶部分类 Tab"
    expect: "页面存在全部、Lite、Pro、World 四个 Tab"

  - id: "SETTINGS_001"
    title: "个人中心展示帮助中心入口"
    tags: ["regression", "settings"]
    precondition: "已登录，网络正常"
    steps:
      - "点击底部个人中心 Tab"
    expect: "页面存在帮助中心按钮"

  - id: "CREATE_001"
    title: "首页创建入口可进入生成视频页面"
    tags: ["regression", "create"]
    precondition: "已登录，网络正常"
    steps:
      - "在首页点击创建按钮"
    expect: "页面存在生成视频按钮"
```

---

## 可选补充

如某条用例有特殊要求，可追加 `notes`：

```yaml
- id: "CASE_XXX"
  title: "特殊场景标题"
  precondition: "已登录"
  steps:
    - "执行某个操作"
  expect: "看到某个结果"
  notes: "这里写特殊说明，例如该页面加载较慢、文案可能中英文不同、需要重点使用 AI 判断。"
```
