---
id: dde.cluster_naming
status: published
owner: io
description: Phase 2 クラスタ命名(全クラスタを一括。batch_size 件ずつ)
tags: [dde, cluster]
model: dde-batch
variables:
  type: object
  required: [n, clusters]
  properties:
    n:        {type: integer}
    # "cluster_id: 代表キーワード" の箇条書き。呼び出し側で組み立てる
    clusters: {type: string}
---
あなたはSEOとアフィリエイトの専門家です。
以下は検索キーワードを意味の近さでクラスタリングした結果です({{ n }}クラスタ)。
各クラスタの代表キーワードを見て、記事テーマとして分かりやすいクラスタ名を付け、
JSON配列**のみ**を出力してください。説明文・コードフェンス・前置きは一切禁止。

スキーマ:
[{"cluster_id": "<入力と完全一致>",
  "name": "20文字以内のテーマ名",
  "description": "40文字以内の説明"}]

命名基準:
- 代表キーワードに共通する検索意図・テーマを、日本語の名詞句で表す
- 個人ブログ・note・アフィリエイト記事のテーマとして使える粒度にする
- 代表キーワードに無い商品名・固有名詞を創作しない

クラスタ一覧(cluster_id: 代表キーワード):
{{ clusters }}
