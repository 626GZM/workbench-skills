"""java-code-index：类/方法/注解解析、泛型参数、Spring 路径拼接、find 通配、
callers、impact 和坏文件容错。全部用临时目录里的假 Java 文件跑，不依赖网络。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'skills' / 'java-code-index' / 'scripts'))

import java_index as ji  # noqa: E402


def run(args):
    """跑一次命令行，返回 (退出码, 解析后的 JSON)。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = ji.main(args)
    text = buf.getvalue().strip()
    return code, (json.loads(text) if text else None)


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return p


CONTROLLER = '''package com.example.order;

import java.util.List;
import java.util.Map;
import lombok.Data;
import com.example.order.service.OrderService;

/**
 * 订单接口。
 * 注释里的 class Fake { void ghost() { } } 不应被解析。
 */
@Data
@RestController
@RequestMapping("/orders")
public class OrderController {

    @Autowired
    private OrderService orderService;

    @GetMapping("/{id}")
    public Order get(@PathVariable("id") Long id) {
        String s = "class Ghost { void ghost() {";
        return orderService.detail(id);
    }

    @PostMapping(value = "/create")
    public Map<String, List<Order>> create(
            @RequestBody CreateOrderRequest request,
            @RequestParam(value = "dryRun", required = false) boolean dryRun) {
        String ignored = "括号 ) 和花括号 } 在字符串里";
        return orderService.save(request, dryRun);
    }

    static class Inner {
        void ping() {
        }
    }
}
'''

SERVICE = '''package com.example.order.service;

public class OrderService {
    public String detail(Long id) {
        return "detail-" + id;
    }

    public String save(Object request, boolean dryRun) {
        return dryRun ? "dry" : "saved";
    }
}
'''

REPOSITORY = '''package com.example.order;

import java.util.List;

public interface OrderRepository<T extends BaseOrder> {

    <R> R map(T order, java.util.function.Function<T, R> fn);

    default List<T> findAll(
            String status,
            int limit) throws IllegalStateException {
        return null;
    }
}

enum OrderStatus {
    NEW, PAID
}

record OrderPair(Order left, Order right) {
}
'''


class ParseTest(unittest.TestCase):
    """单文件解析：类、方法、注解、泛型、内部类。"""

    def test_class_methods_and_annotations(self):
        classes = ji.parse_java(CONTROLLER, 'src/main/java/com/example/order/OrderController.java')
        names = [c['fqn'] for c in classes]
        self.assertEqual(names, ['com.example.order.OrderController',
                                 'com.example.order.OrderController.Inner'])
        cls = classes[0]
        self.assertEqual(cls['kind'], 'class')
        self.assertEqual(cls['package'], 'com.example.order')
        self.assertEqual(cls['line'], 15)
        self.assertIn('com.example.order.service.OrderService', cls['imports'])
        self.assertEqual(ji.annotation_names(cls['annotations']),
                         ['Data', 'RestController', 'RequestMapping'])
        mapping = [a for a in cls['annotations'] if a['name'] == 'RequestMapping'][0]
        self.assertEqual(mapping['args'], '"/orders"')

        methods = {m['name']: m for m in cls['methods']}
        self.assertEqual(sorted(methods), ['create', 'get'])

        get = methods['get']
        self.assertEqual(get['returns'], 'Order')
        self.assertEqual(get['params'], [{'type': 'Long', 'name': 'id'}])
        self.assertEqual(get['line'], 21)
        self.assertEqual(ji.annotation_names(get['annotations']), ['GetMapping', 'PathVariable'])
        self.assertEqual(get['annotations'][0]['args'], '"/{id}"')

        create = methods['create']
        self.assertEqual(create['returns'], 'Map')
        self.assertEqual([p['type'] for p in create['params']],
                         ['CreateOrderRequest', 'boolean'])
        self.assertEqual([p['name'] for p in create['params']], ['request', 'dryRun'])
        self.assertIn('Map create(CreateOrderRequest request, boolean dryRun)',
                      create['signature'])

        # 注释和字符串里的假类不能出现
        self.assertNotIn('Ghost', ' '.join(names))

        inner = classes[1]
        self.assertEqual(inner['kind'], 'class')
        self.assertEqual([m['name'] for m in inner['methods']], ['ping'])

    def test_generics_interface_enum_record(self):
        classes = ji.parse_java(REPOSITORY, 'src/main/java/com/example/order/OrderRepository.java')
        by_name = {c['fqn'].rsplit('.', 1)[-1]: c for c in classes}
        self.assertEqual(sorted(by_name), ['OrderPair', 'OrderRepository', 'OrderStatus'])
        self.assertEqual(by_name['OrderRepository']['kind'], 'interface')
        self.assertEqual(by_name['OrderStatus']['kind'], 'enum')
        self.assertEqual(by_name['OrderPair']['kind'], 'record')

        repo = by_name['OrderRepository']
        map_m = [m for m in repo['methods'] if m['name'] == 'map'][0]
        self.assertEqual(map_m['returns'], 'R')
        self.assertEqual([p['type'] for p in map_m['params']],
                         ['T', 'java.util.function.Function<T, R>'])

        find_m = [m for m in repo['methods'] if m['name'] == 'findAll'][0]
        self.assertEqual(find_m['returns'], 'List')
        self.assertEqual([p['name'] for p in find_m['params']], ['status', 'limit'])
        self.assertIn('default', find_m['modifiers'])


class CommandTest(unittest.TestCase):
    """基于临时仓库跑 build / find / callers / api / impact。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        base = 'src/main/java/com/example/order/'
        write(self.repo, base + 'OrderController.java', CONTROLLER)
        write(self.repo, base + 'service/OrderService.java', SERVICE)
        code, out = run(['build', '--repo', self.repo, '--out', 'idx'])
        self.assertEqual(code, 0)
        self.out = out

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_writes_index(self):
        self.assertEqual(self.out['stats']['files'], 2)
        self.assertEqual(self.out['stats']['classes'], 3)   # 含内部类 Inner
        self.assertEqual(self.out['stats']['parse_errors'], 0)
        index_path = Path(self.repo) / 'idx' / 'index.json'
        self.assertTrue(index_path.is_file())
        data = json.loads(index_path.read_text(encoding='utf-8'))
        self.assertEqual(data['src'], 'src/main/java')
        self.assertEqual(len(data['classes']), 3)
        self.assertEqual(data['parse_errors'], [])
        # 所有公开字段都在，内部字段（_body）不落盘
        for cls in data['classes']:
            for method in cls['methods']:
                self.assertNotIn('_body', method)
                self.assertIn('signature', method)

    def test_find_substring_and_wildcard(self):
        code, out = run(['find', '--repo', self.repo, '--out', 'idx', '--query', 'detail'])
        self.assertEqual(code, 0)
        self.assertEqual(out['count'], 1)
        hit = out['hits'][0]
        self.assertEqual(hit['kind'], 'method')
        self.assertEqual(hit['class'], 'com.example.order.service.OrderService')
        self.assertTrue(hit['location'].endswith('OrderService.java:%d' % hit['line']))

        code, out = run(['find', '--repo', self.repo, '--out', 'idx', '--query', '*Controller'])
        self.assertEqual(code, 0)
        self.assertEqual([h['name'] for h in out['hits']], ['OrderController'])

        # 注解名也参与匹配，@ 会被忽略
        code, out = run(['find', '--repo', self.repo, '--out', 'idx',
                         '--query', '@Rest*'])
        self.assertEqual(code, 0)
        self.assertEqual([h['fqn'] for h in out['hits']],
                         ['com.example.order.OrderController'])

        code, out = run(['find', '--repo', self.repo, '--out', 'idx', '--query', 'Get*'])
        self.assertEqual([h['name'] for h in out['hits']], ['get'])

    def test_callers(self):
        code, out = run(['callers', '--repo', self.repo, '--out', 'idx',
                         '--method', 'OrderService.detail'])
        self.assertEqual(code, 0)
        self.assertEqual([t['class'] for t in out['targets']],
                         ['com.example.order.service.OrderService'])
        self.assertEqual(out['count'], 1)
        caller = out['callers'][0]
        self.assertEqual(caller['class'], 'com.example.order.OrderController')
        self.assertEqual(caller['method'], 'get')
        self.assertIn('orderService.detail(id)', caller['text'])
        self.assertEqual(caller['location'],
                         'src/main/java/com/example/order/OrderController.java:%d' % caller['line'])
        self.assertIn('启发式', out['note'])

    def test_api_join_paths(self):
        code, out = run(['api', '--repo', self.repo, '--out', 'idx'])
        self.assertEqual(code, 0)
        self.assertEqual(out['count'], 2)
        ctrl = out['controllers'][0]
        self.assertEqual(ctrl['class'], 'com.example.order.OrderController')
        self.assertEqual(ctrl['base_path'], '/orders')
        got = {(e['http'], e['path']) for e in ctrl['endpoints']}
        self.assertEqual(got, {('GET', '/orders/{id}'), ('POST', '/orders/create')})
        post = [e for e in ctrl['endpoints'] if e['http'] == 'POST'][0]
        self.assertEqual(post['method'], 'create')
        self.assertIn('POST create', 'POST ' + post['method'])
        self.assertEqual(post['annotations'], ['PostMapping', 'RequestBody', 'RequestParam'])

        code, out = run(['api', '--repo', self.repo, '--out', 'idx', '--path', '/orders/create'])
        self.assertEqual(out['count'], 1)
        self.assertEqual(out['controllers'][0]['endpoints'][0]['path'], '/orders/create')

        code, out = run(['api', '--repo', self.repo, '--out', 'idx', '--path', '/other'])
        self.assertEqual(out['count'], 0)
        self.assertEqual(out['controllers'], [])

    def test_impact(self):
        code, out = run(['impact', '--repo', self.repo, '--out', 'idx',
                         '--file', 'src/main/java/com/example/order/service/OrderService.java'])
        self.assertEqual(code, 0)
        self.assertEqual([c['fqn'] for c in out['classes']],
                         ['com.example.order.service.OrderService'])
        reasons = {r['reason'] for r in out['impact']}
        self.assertIn('import', reasons)
        self.assertIn('call', reasons)
        calls = {(r['method'], r['detail']) for r in out['impact'] if r['reason'] == 'call'}
        self.assertEqual(calls, {('get', 'detail'), ('create', 'save')})
        self.assertIn('可能漏报', out['note'])


class ParseErrorTest(unittest.TestCase):
    """坏文件只进 parse_errors，不影响其它文件入索引。"""

    def test_bad_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as repo:
            base = 'src/main/java/com/example/order/'
            write(repo, base + 'Good.java',
                  'package com.example.order;\n\npublic class Good {\n'
                  '    public void ok() {\n    }\n}\n')
            write(repo, base + 'Broken.java',
                  'package com.example.order;\n\npublic class Broken {\n'
                  '    void f() {\n')
            binary = Path(repo) / base / 'Binary.java'
            binary.write_bytes(b'\x00\x01\x02package com.example.order;')

            code, out = run(['build', '--repo', repo, '--out', 'idx'])
            self.assertEqual(code, 0)
            self.assertEqual(out['stats']['files'], 3)
            self.assertEqual(out['stats']['classes'], 1)
            self.assertEqual(out['stats']['parse_errors'], 2)
            bad_files = {e['file'] for e in out['parse_errors']}
            self.assertIn('src/main/java/com/example/order/Broken.java', bad_files)
            self.assertIn('src/main/java/com/example/order/Binary.java', bad_files)
            for err in out['parse_errors']:
                self.assertTrue(err['error'])

            data = json.loads((Path(repo) / 'idx' / 'index.json').read_text(encoding='utf-8'))
            self.assertEqual([c['fqn'] for c in data['classes']],
                             ['com.example.order.Good'])


if __name__ == '__main__':
    unittest.main()
