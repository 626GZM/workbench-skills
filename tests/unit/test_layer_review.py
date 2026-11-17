"""java-layer-review：只报新增行、12 条规则各命中、规则可关、severity 可调、配置回读。"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'skills' / 'java-layer-review' / 'scripts'))

import layer_review as lr  # noqa: E402


def run(args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = lr.main(args)
    text = buf.getvalue().strip()
    return code, (json.loads(text) if text else None)


HELPER_OLD = """package com.example.order;

public class OrderStatusHelper {
    public boolean paid(String status) {
        if ("PAID".equals(status)) {
            return true;
        }
        return false;
    }
}
"""

HELPER_NEW = """package com.example.order;

public class OrderStatusHelper {
    public boolean paid(String status) {
        if ("PAID".equals(status)) {
            return true;
        }
        return false;
    }

    public boolean closed(String status) {
        return "CLOSED".equals(status);
    }
}
"""

HELPER_TEST = """package com.example.order;

public class OrderStatusHelperTest {
    boolean isPaid(String status) {
        return "PAID".equals(status);
    }
}
"""

ENTITY = """package com.example.order.entity;

public class OrderEntity {
    private String status;

    public String getStatus() {
        if (status == null) {
            return "";
        }
        return status;
    }
}
"""

CONTROLLER = """package com.example.order.controller;

public class OrderController {
    private final OrderMapper orderMapper;

    public Result create(OrderCreateRequest req) {
        if (getCurrentUser() == null) {
            return Result.fail("not login");
        }
        try {
            for (OrderItem item : req.getItems()) {
                orderMapper.insert(item);
            }
        } catch (Exception e) {
            log.error("create failed", e);
        }
        if (req.getProductId() == null) {
            throw new IllegalArgumentException("productId required");
        }
        if ("PAID".equals(req.getStatus())) {
            return Result.ok();
        }
        return Result.ok();
    }
}
"""

SERVICE = """package com.example.order.service;

import java.util.Date;
import java.util.List;

public class OrderService {
    private final OrderMapper orderMapper;

    @Transactional
    public void pay(Long id) {
        try {
            orderMapper.updateStatus(id);
        } catch (Exception e) {
            log.warn("ignore", e);
        }
        Order order = new Order();
        order.setCreateTime(new Date());
    }

    public OrderEntity getDetail(Long id) {
        return orderMapper.selectById(id);
    }

    public void batch(List<Long> ids) {
        for (Long id : ids) {
            orderMapper.selectById(id);
        }
    }

    public String buildSql(String status) {
        String sql = "select id from t_order where status = '" + status + "'";
        return sql;
    }
}
"""

ALL_FILES = {
    'src/main/java/com/example/order/entity/OrderEntity.java': ENTITY,
    'src/main/java/com/example/order/controller/OrderController.java': CONTROLLER,
    'src/main/java/com/example/order/service/OrderService.java': SERVICE,
}


@unittest.skipUnless(shutil.which('git'), '需要 git')
class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'tester@example.com')
        self.git('config', 'user.name', 'Tester')

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.tmp)] + list(args),
                              capture_output=True, text=True, check=True)

    def write(self, rel, text):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def commit(self, message='init'):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def write_all(self):
        for rel, text in ALL_FILES.items():
            self.write(rel, text)

    def review(self, *extra):
        return run(['review', '--repo', str(self.tmp), '--uncommitted'] + list(extra))


class AddedOnlyTest(Base):
    def test_只报新增行且跳过测试与注释(self):
        self.write('src/main/java/com/example/order/OrderStatusHelper.java', HELPER_OLD)
        self.commit()
        # 旧行保留，新增一个同样写法的关闭判断
        self.write('src/main/java/com/example/order/OrderStatusHelper.java', HELPER_NEW)
        # 测试代码里的同类写法不报
        self.write('src/test/java/com/example/order/OrderStatusHelperTest.java', HELPER_TEST)
        # 非 Java 文件不扫
        self.write('notes.txt', '"PAID".equals(status)\n')

        code, hits = self.review()
        self.assertEqual(code, 0)
        files = {h['file'] for h in hits}
        self.assertEqual(files, {'src/main/java/com/example/order/OrderStatusHelper.java'})

        magic = [h for h in hits if h['rule'] == 'magic-string-compare']
        self.assertEqual([h['line'] for h in magic], [12])
        self.assertIn('CLOSED', magic[0]['snippet'])
        self.assertNotIn(5, [h['line'] for h in magic])  # 旧行不报

        self.assertEqual(set(hits[0]),
                         {'rule', 'severity', 'file', 'line', 'snippet', 'why', 'fix'})


class RuleHitTest(Base):
    def test_12条规则各至少命中一条(self):
        self.write('README.md', '# demo\n')
        self.commit()
        self.write_all()

        code, hits = self.review()
        self.assertEqual(code, 0)
        ids = {h['rule'] for h in hits}
        self.assertEqual(ids, {r['id'] for r in lr.RULE_SPECS})
        self.assertEqual(len(lr.RULE_SPECS), 12)

        for hit in hits:
            self.assertGreater(hit['line'], 0)
            self.assertTrue(hit['snippet'])
            self.assertTrue(hit['why'])
            self.assertTrue(hit['fix'])
            self.assertIn(hit['severity'], lr.SEVERITIES)

    def test_base到head的区间也能查(self):
        self.write('README.md', '# demo\n')
        self.commit()
        self.write_all()
        self.commit('新增分层代码')

        code, hits = run(['review', '--repo', str(self.tmp), '--base', 'HEAD~1', '--head', 'HEAD'])
        self.assertEqual(code, 0)
        self.assertEqual({h['rule'] for h in hits}, {r['id'] for r in lr.RULE_SPECS})

    def test_基准不存在时报错(self):
        self.write('README.md', '# demo\n')
        self.commit()
        code, out = run(['review', '--repo', str(self.tmp), '--base', 'origin/nope'])
        self.assertEqual(code, 2)
        self.assertIn('error', out)


class ConfigTest(Base):
    def setUp(self):
        super().setUp()
        self.write('README.md', '# demo\n')
        self.commit()
        self.write_all()

    def init_config(self):
        path = self.tmp / 'rules.json'
        code, out = run(['rules', 'init', '--out', str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(out['rules'], 12)
        return path

    def test_规则可以关闭(self):
        path = self.init_config()
        cfg = json.loads(path.read_text(encoding='utf-8'))
        cfg['rules']['controller-direct-dao']['enabled'] = False
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')

        code, hits = self.review('--rules', str(path))
        self.assertEqual(code, 0)
        ids = {h['rule'] for h in hits}
        self.assertNotIn('controller-direct-dao', ids)
        self.assertIn('n-plus-one-query', ids)

    def test_severity可调且非法值报错(self):
        path = self.init_config()
        cfg = json.loads(path.read_text(encoding='utf-8'))
        cfg['rules']['manual-timestamp']['severity'] = 'high'
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')

        code, hits = self.review('--rules', str(path))
        self.assertEqual(code, 0)
        self.assertEqual({h['severity'] for h in hits if h['rule'] == 'manual-timestamp'},
                         {'high'})

        cfg['rules']['manual-timestamp']['severity'] = '致命'
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
        code, out = self.review('--rules', str(path))
        self.assertEqual(code, 2)
        self.assertIn('severity', out['error'])

    def test_init出的文件能被review读回(self):
        path = self.init_config()
        cfg = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(set(cfg['rules']), {r['id'] for r in lr.RULE_SPECS})
        for item in cfg['rules'].values():
            self.assertTrue(item['enabled'])
            self.assertIn(item['severity'], lr.SEVERITIES)

        _, default_hits = self.review()
        _, with_config = self.review('--rules', str(path))
        self.assertEqual(default_hits, with_config)

    def test_rules清单与说明(self):
        code, listed = run(['rules', 'list'])
        self.assertEqual(code, 0)
        self.assertEqual(len(listed), 12)
        self.assertEqual({r['id'] for r in listed}, {r['id'] for r in lr.RULE_SPECS})

        code, one = run(['rules', 'explain', 'n-plus-one-query'])
        self.assertEqual(code, 0)
        self.assertEqual(one['id'], 'n-plus-one-query')
        self.assertTrue(one['patterns'])

        code, err = run(['rules', 'explain', 'no-such-rule'])
        self.assertEqual(code, 2)
        self.assertIn('error', err)

    def test_重复init默认不覆盖(self):
        path = self.init_config()
        code, out = run(['rules', 'init', '--out', str(path)])
        self.assertEqual(code, 2)
        self.assertIn('已存在', out['error'])
        code, _ = run(['rules', 'init', '--out', str(path), '--force'])
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
