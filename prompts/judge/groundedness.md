---
id: judge.groundedness
status: published
owner: io
description: 生成文が context に帰属しているかを採点する(19章 §6 Grounding)
tags: [judge, groundedness]
model: judge
includes: [judge_output]
variables:
  type: object
  required: [context, output]
  properties:
    context: {type: string}
    output:  {type: string}
---
あなたは事実確認の担当者です。以下の「生成文」が「参照可能な事実(context)」に
基づいて書かれているかを採点してください。

# 参照可能な事実(context)
{{ context }}

# 生成文
{{ output }}

採点の基準:
- 1.0 = 生成文の記述がすべて context に帰属する
- 0.5 = context に無い記述が含まれるが、断定を避けた一般論にとどまっている
- 0.0 = context に無い固有名詞・数値・年号・統計を断定して書いている
- **文体の好み・読みやすさは採点対象にしない**(別の指標で見ている)
- violations には、context に帰属しない記述を**生成文から引用して**列挙する

{{ include.judge_output }}
