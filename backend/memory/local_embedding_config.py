"""Pinned public embedding artefact; no runtime download or provider keys."""
MODEL = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
FILES = ["config.json", "config_sentence_transformers.json", "modules.json",
         "sentence_bert_config.json", "1_Pooling/config.json", "model.safetensors",
         "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json", "vocab.txt"]
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
