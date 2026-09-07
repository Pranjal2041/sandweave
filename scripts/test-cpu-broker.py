#!/usr/bin/env python3
import unittest
import cpu_broker

class Rates(unittest.TestCase):
    def job(self, weight, quota=None):
        return cpu_broker.Job({'root': 1, 'weight': weight, 'quota': quota})

    def test_weighted(self):
        a, b = self.job(300), self.job(100)
        self.assertEqual(cpu_broker.allocate_rates([a, b], 4), {a: 3, b: 1})

    def test_redistribute_capacity_unused_by_capped_job(self):
        a, b = self.job(100, 1), self.job(100)
        self.assertEqual(cpu_broker.allocate_rates([a, b], 4), {a: 1, b: 3})

    def test_caps_leave_capacity_available(self):
        a, b = self.job(300, 1), self.job(100, 1)
        self.assertEqual(cpu_broker.allocate_rates([a, b], 4), {a: 1, b: 1})

if __name__ == '__main__':
    unittest.main()
