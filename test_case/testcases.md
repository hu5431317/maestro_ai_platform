---
# ── 默认配置：此文件所有用例共用 ──
# device 填名称（匹配 devices.yaml 中 name 字段），自动解析为 adb ID
# 也可直接填 adb ID（如 8073fcda），同样生效
app_id: "@flyu"          # 应用别名（在 config/apps.yaml 中注册）
platform: android        # android / ios
device: "oppo A32"       # devices.yaml 中的设备名称 → 自动解析为 avd: 8073fcda
---

## 登录模块
打开App，点击邮箱登录按钮，输入账号cc123@qq.com和密码123456，验证登陆状态是否登陆成功

## 进入MV studio页面
登录后点击底部MV studioTab，验证页面是否存在四个tab，分别是全部，lite，pro，world

## 设置页面
登录后点击底部个人中心tab，验证页面是否存在帮助中心按钮

## 创建功能
在首页点击创建按钮，验证页面是否存在生成视频按钮
