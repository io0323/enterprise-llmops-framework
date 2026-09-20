---
id: dde.expand
status: published
owner: io
description: FR-002 関連キーワード展開(シード1件につき1回で最大n件)
tags: [dde, expand]
model: dde-batch
variables:
  type: object
  required: [seed, max_keywords, existing_count, existing]
  properties:
    seed:           {type: string, minLength: 1}
    max_keywords:   {type: integer}
    existing_count: {type: integer}
    # 既出キーワードの箇条書き。0件なら "(なし)"。
    # 件数で文面が変わるため呼び出し側で組み立てる(テンプレートに制御構文を持たせない)
    existing:       {type: string}
---
あなたはSEOとアフィリエイトの専門家です。
シードキーワード「{{ seed }}」に関連する検索キーワードを最大{{ max_keywords }}件生成し、
JSON配列**のみ**を出力してください。説明文・コードフェンス・前置きは一切禁止。

条件:
- 実際に検索されうる日本語の検索クエリであること
- 類義語・派生テーマ・悩み・比較軸・購入検討語をバランスよく含めること
- 既出のキーワードは除外すること

既出キーワード({{ existing_count }}件):
{{ existing }}

スキーマ:
["キーワード1", "キーワード2", ...]
