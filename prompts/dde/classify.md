---
id: dde.classify
status: published
owner: io
description: FR-003 + FR-004 意図分類と商用性の統合バッチ判定
tags: [dde, classify]
model: dde-batch
variables:
  type: object
  required: [n, keywords]
  properties:
    n:        {type: integer}
    # キーワードの箇条書き。呼び出し側で組み立てる
    keywords: {type: string}
---
あなたはSEOとアフィリエイトの専門家です。
評価対象の収益化モデルは、個人ブログ・note・アフィリエイトへの自然流入による収益化です。
以下のキーワードリスト({{ n }}件)を分析し、JSON配列**のみ**を出力してください。
説明文・コードフェンス・前置きは一切禁止。

スキーマ:
[{"keyword": "<入力と完全一致>",
  "intents": {"情報収集|比較|購入|問題解決|学習|体験共有": 0.0-1.0の上位2つ},
  "seo_intent": "Know|Do|Buy|Go",
  "purchase_intent": 0-100,
  "problem_depth": 0-100,
  "note_potential": 0-100}]

判定基準:
- purchase_intent: 個人読者が記事経由で商品購入・有料note購入・アフィリエイト経由申込に
  至る可能性を評価する。法人の予算執行やB2B導入検討は、このモデルでは収益化できないため
  高評価しない
- problem_depth: 解決しないと困る度合い(深いほど高CV)
- note_potential: 体験談・ノウハウとして有料販売が成立しうるか

キーワードリスト:
{{ keywords }}
