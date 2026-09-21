---
id: judge.readability
status: published
owner: io
description: 生成文の読みやすさを採点する(文体の統一・冗長さ・構造)
tags: [judge, readability]
model: judge
includes: [judge_output]
variables:
  type: object
  required: [output]
  properties:
    output: {type: string}
---
あなたは編集者です。以下の「生成文」の読みやすさを採点してください。

# 生成文
{{ output }}

採点の基準:
- 1.0 = 文体が統一され、同じ語尾や同じ主張の繰り返しが無く、構造が追える
- 0.5 = 読めるが、語尾の単調さ・冗長な言い換え・構造の飛びがある
- 0.0 = 文体が混在している、または同じ内容を繰り返していて読み進められない
- **事実の正しさ・指示への適合は採点対象にしない**(別の指標で見ている)
- violations には、問題のある箇所を生成文から引用して列挙する

{{ include.judge_output }}
