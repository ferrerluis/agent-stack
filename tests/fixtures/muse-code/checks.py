import unittest
from calculator import add

class AdditionTest(unittest.TestCase):
    def test_integers(self):
        for lhs, rhs in ((2, 3), (0, 7), (-4, 9), (17, -8), (-3, -5), (0, 0)):
            with self.subTest(lhs=lhs, rhs=rhs):
                self.assertEqual(add(lhs, rhs), lhs + rhs)

if __name__ == "__main__":
    unittest.main()
