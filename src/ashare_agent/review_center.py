"""Legacy historical viewer. New writes are retired; v2 owns prospective AI research."""
import json

from .forward_lab import ForwardLab


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ReviewCenter:
    def __init__(self, root):
        self.book = ForwardLab(root / 'runtime/daily-lab/observations')
        self.db = self.book.db
        self.db.execute('CREATE TABLE IF NOT EXISTS review_imports (batch_id TEXT, mode TEXT, digest TEXT, received_at TEXT, payload TEXT, arms TEXT, prospective INTEGER, PRIMARY KEY(batch_id,mode))')
        self.db.commit()

    def close(self):
        self.book.close()

    def batch(self, batch_id):
        row = self.db.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if row is None:
            raise ValueError('冻结批次不存在，请刷新列表')
        return row

    def packet(self, batch_id):
        batch = self.batch(batch_id)
        candidates = json.loads(batch['selection_json'])['candidates'][:20]
        return dict(schema_version=1, batch_id=batch_id, as_of=batch['as_of'],
                    input_hash=batch['input_hash'], frozen_at=batch['created_at'],
                    candidates=candidates, evidence=json.loads(batch['evidence_json']),
                    instructions='第一版历史输入只读。禁止将旧批次重新包装成新时点前瞻材料。新研究请打开 /research。')

    def import_results(self, batch_id, values, *, save=True):
        raise ValueError("legacy_review_write_retired：历史记录保持原样；新实验使用 /research 或 research-import，不再接受第一版前瞻导入。")

    def state(self):
        summary = self.book.summary()
        summary.update(version="legacy_v1_read_only", prospective_writes_retired=True,
                       warning="仅保存第一版原始事实；未按第二版协议重新认证，不计入新版研究结论。")
        for batch in summary['batches']:
            packet = self.packet(batch['batch_id'])
            batch['candidates'] = packet['candidates']
            batch['reviews'] = {r['mode']: dict(payload=json.loads(r['payload']),
                                received_at=r['received_at'], prospective=bool(r['prospective']),
                                selection=json.loads(r['arms'])) for r in self.db.execute(
                                    'SELECT * FROM review_imports WHERE batch_id=?', (batch['batch_id'],))}
        return summary


def effective_arms(db, batch):
    arms = json.loads(batch['arms_json'])
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_imports'").fetchone()
    if exists:
        for row in db.execute('SELECT mode,arms FROM review_imports WHERE batch_id=? AND prospective=1', (batch['batch_id'],)):
            arms['ai_' + row['mode']] = json.loads(row['arms'])
    return arms
