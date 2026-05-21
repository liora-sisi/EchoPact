#!/usr/bin/env python3
"""
Echo Pact 历史对话批量导入脚本
支持断点续传、进度条
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from backend.utils.db import init_db, get_conn
from backend.memory.models import Memory
from backend.memory.crud import create_memory
from backend.memory.vector_store import upsert_memory

CHECKPOINT_FILE = "/opt/echo-pact/import_checkpoint.json"

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"last_index": 0, "total": 0}

def save_checkpoint(index, total):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"last_index": index, "total": total}, f)

def parse_conversations(filepath: str):
    """解析对话文件，返回记忆列表——待实现"""
    # TODO: 根据实际导出格式解析
    # 现在返回空列表作为框架
    return []

def import_memories(filepath: str, agent_id: str = "default"):
    init_db()
    checkpoint = load_checkpoint()
    start = checkpoint["last_index"]
    
    memories = parse_conversations(filepath)
    total = len(memories)
    
    if total == 0:
        print("没有找到可导入的记忆，请检查文件格式")
        return
    
    print(f"共 {total} 条记忆，从第 {start} 条开始导入...")
    
    for i, mem_data in enumerate(memories[start:], start=start):
        try:
            # 去重检查（时间+内容）
            with get_conn() as conn:
                existing = conn.execute(
                    "SELECT id FROM memories WHERE content=? AND created_at=? AND agent_id=?",
                    (mem_data.get("content", ""), mem_data.get("created_at", ""), agent_id)
                ).fetchone()
            if existing:
                print(f"\r跳过重复第{i+1}条", end="")
                save_checkpoint(i + 1, total)
                continue
            mem = Memory(
                content=mem_data.get("content", ""),
                valence=mem_data.get("valence", 0.0),
                arousal=mem_data.get("arousal", 0.0),
                source_type=mem_data.get("source_type", "user"),
                agent_id=agent_id
            )
            mid = create_memory(mem)
            upsert_memory(mid, mem.content, agent_id)
            
            save_checkpoint(i + 1, total)
            
            # 进度条
            progress = (i + 1) / total * 100
            print(f"\r导入进度: {i+1}/{total} ({progress:.1f}%)", end="")
            
            time.sleep(0.1)  # 避免API限流
            
        except Exception as e:
            print(f"\n第{i}条导入失败: {e}")
            save_checkpoint(i, total)
            break
    
    print(f"\n导入完成！成功导入 {total} 条记忆")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/import_history.py <对话文件路径>")
        sys.exit(1)
    
    filepath = sys.argv[1]
    agent_id = sys.argv[2] if len(sys.argv) > 2 else "default"
    import_memories(filepath, agent_id)
