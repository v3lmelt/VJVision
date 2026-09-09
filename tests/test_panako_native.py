"""Guard the limited upstream storage patch against changing query behavior."""
import unittest
from tools.build_panako_worker import patch_storage


class PanakoNativePatchTests(unittest.TestCase):
    def source(self):
        return '''before .setMapSize(1024l * 1024l * 1024l * 1024l)
public void processDeleteQueue() {
if (storeQueue.isEmpty()) return;
if (!storeQueue.containsKey(id)) return;
queue = storeQueue.get(id);
}
public void addToQueryQueue(long hash) { queryQueue.add(hash); }
'''

    def test_storage_patch_preserves_query_implementation(self):
        original = self.source()
        patched = patch_storage(original)
        self.assertEqual(patched.split("public void addToQueryQueue")[1],
                         original.split("public void addToQueryQueue")[1])
        self.assertIn('Long.getLong("vjvision.panako.map.bytes", 256L * 1024L * 1024L)', patched)
        self.assertIn("queue = deleteQueue.get(id)", patched)

    def test_unknown_upstream_layout_is_rejected(self):
        for changed in [self.source().replace(".setMapSize", ".setNewMapSize"),
                        self.source().replace("queue = storeQueue.get(id)", "queue = somethingElse.get(id)")]:
            with self.assertRaises(ValueError):
                patch_storage(changed)


if __name__ == "__main__":
    unittest.main()
