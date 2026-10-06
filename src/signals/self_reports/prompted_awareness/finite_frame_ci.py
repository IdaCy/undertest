"""Exact equal-tail hypergeometric inversion for fixed finite-frame prompted reports."""
from math import comb

def mass(N,K,n,x):
 if not(0<=x<=K and 0<=n-x<=N-K):return 0
 return comb(K,x)*comb(N-K,n-x)
def interval(N,n,x):
 if not(0<=x<=n<=N):raise ValueError('invalid_integer_counts')
 den=comb(N,n);accepted=[]
 for K in range(N+1):
  upper=sum(mass(N,K,n,z)for z in range(x,n+1));lower=sum(mass(N,K,n,z)for z in range(0,x+1))
  if 40*upper>=den and 40*lower>=den:accepted.append(K)
 return [min(accepted),max(accepted)]
def combined(Nfull,Nremaining,n,yes,uncertain,certainty_yes,certainty_uncertain):
 if not(0<=yes<=yes+uncertain<=n and 0<=certainty_yes<=certainty_yes+certainty_uncertain<=Nfull-Nremaining):raise ValueError('invalid_coding_counts')
 candidates=[interval(Nremaining,n,x)for x in range(yes,yes+uncertain+1)]
 low=certainty_yes+min(x[0]for x in candidates);high=certainty_yes+certainty_uncertain+max(x[1]for x in candidates)
 return dict(total_affirmative_count_bounds=[low,high],finite_frame_rate_bounds=[low/Nfull,high/Nfull],point_estimate=(certainty_yes+Nremaining*yes/n)/Nfull if uncertain==certainty_uncertain==0 else None,target='Fixed completed-original saved frame under this exact prompted retrospective self-report/coding protocol; not latent awareness or re-elicitation variability.',confidence_level=.95,unresolved_retained_as_sensitivity=bool(uncertain or certainty_uncertain))
