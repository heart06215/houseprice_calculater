import unittest
from unittest.mock import patch
from inverse_price_engine import propagate_price,derive_from_nearest_anchor,PriceInputs
from engine_policy import gate,overlay,fingerprint,load_policy

class EnginePolicyTests(unittest.TestCase):
    def test_forward_backward_formula(self):
        indices={(2026,2):110,(2026,3):90}
        self.assertEqual(propagate_price(10000,(2026,1),(2026,3),indices),9900)
        self.assertEqual(propagate_price(9900,(2026,3),(2026,1),indices),10000)
        self.assertIsNone(propagate_price(10000,(2026,3),(2026,1),{(2026,3):90}))

    def test_invalid_inputs(self):
        for price in [True,-1,0,float('nan'),float('inf')]:
            self.assertIsNone(propagate_price(price,(2026,1),(2026,1),{}))
        self.assertIsNone(propagate_price(100,(2026,1),(2026,2),{(2026,2):True}))

    def test_missing_interval_keeps_prior_anchor(self):
        result=derive_from_nearest_anchor('上海',{('上海',2025,12):42000,('上海',2026,8):40000},{('上海',2026,m):100 for m in range(1,9)})
        self.assertTrue(all(result[2026,m]['price']==42000 for m in range(1,8)))

    def test_gate(self):
        self.assertEqual(gate([1]*5,[2]*5),'insufficient_samples')
        self.assertEqual(gate([9]*12,[10]*12),'excess_error')
        self.assertEqual(gate([2]*12,[1]*12),'worse_than_constant_anchor')
        self.assertEqual(gate([1]*12,[2]*12),'passed')
        self.assertEqual(gate([0]*9+[10,10,10],[15]*12),'recent_drift')
        self.assertEqual(gate([4]*12,[8]*12,[4]*12),'recent_bias')
        self.assertEqual(gate([4]*12,[8]*12,[-4]*12),'recent_bias')

    def test_overlay_preserves_observation_and_rejects_failed_city(self):
        obs={('北京',2026,1):90};derived={(2026,1):{'price':90}}
        policy={'official_verified':True,'input_fingerprint':'abc','cities':{'北京':dict(status='passed',anchor_date='2025-08',predictions={'2026-01':95},mape_pct=1)}}
        overlay('北京',derived,policy,obs)
        self.assertEqual(derived[2026,1]['price'],90)
        self.assertEqual(derived[2026,1]['model_comparison']['price'],95)
        self.assertFalse(derived[2026,1]['model_comparison']['used_for_ranking'])
        self.assertEqual(obs[('北京',2026,1)],90)
        policy['cities']['北京']['status']='excess_error'
        untouched={(2026,1):{'price':90}}
        overlay('北京',untouched,policy,obs)
        self.assertEqual(untouched[2026,1]['price'],90)

    def test_old_model_never_overrides_nearer_gap_anchor(self):
        derived={(2026,1):dict(price=80,method='index_estimate',anchor_date='2025-12')}
        report={'official_verified':True,'input_fingerprint':'abc','cities':{'北京':dict(status='passed',anchor_date='2025-08',predictions={'2026-01':100})}}
        overlay('北京',derived,report,{})
        self.assertEqual(derived[2026,1]['price'],80)
        self.assertEqual(derived[2026,1]['anchor_date'],'2025-12')

    def test_changed_input_invalidates_policy(self):
        obs=PriceInputs();obs['北京',2025,8]=100
        before=fingerprint(obs,{})
        obs['北京',2025,8]=101
        self.assertNotEqual(before,fingerprint(obs,{}))
        import json
        with patch('pathlib.Path.read_text',return_value=json.dumps({'version':1,'input_fingerprint':before,'cities':{'北京':{'status':'passed'}}})):
            self.assertEqual(load_policy(obs,{}),{'cities':{}})

if __name__=='__main__':unittest.main()
