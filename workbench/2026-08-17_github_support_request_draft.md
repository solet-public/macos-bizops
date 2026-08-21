# GitHub Support request — paste into https://support.github.com (LOCAL-ONLY file)

Subject: Remove cached/unreachable objects after sensitive-data history rewrite

Repository: BranchMetrics/bizops-knowledge-base (private)

We removed sensitive data (bulk CRM exports containing personal information)
from this repository by rewriting history with git-filter-repo and
force-pushing all branches on 2026-08-17. Per your documented procedure for
sensitive-data removal, please:

1. Remove the now-unreachable objects from the repository on GitHub's side
   and clear any cached views/CDN copies.
2. In particular, the following 28 blob SHAs contained the sensitive data
   and must become permanently inaccessible (git/blobs API and web URLs):

02aa5e181367d1b403488dea5460a04f29a9fea8, a66efed3dc21cddcd36c4d09fab2bd2adc866ffb,
2fa6e324c433e31f0c2397336727c8c2173f784c, eb63123efc938384efd23a36b8176ee1180caa17,
2d189fb2d88199de3b029cf5bd29cd72fc9331af, ac99e4e2a789a8f13aa569a6d9585cad5c72a0e7,
f83505c5848d2b75e7bae9ec2cb06411b91e9a3b, 5a3204707dd95d6e3bd18f815971ad746bb1b6d9,
670d9c7ec9b98df3a5d19eaefe8f3da3b965c81b, 29538ab72ed83d5a4f39e18fa303f42a1dfc97b6,
c86b197040578aa06bfefc92ae0408372479bca4, 24dbf47294f361da7daf1f9c19277446fce1d034,
9591a7ebbf3ebcf83420b945c36079dbc1bd19fc, 6aa50a8be9d4d15a19911bf52b6c805d8bbee2e4,
6716d5566dcadce7bb1c9119a519c12991af4ac1, ceef7633fead40029c7c8c3773607015d77de1a3,
71133758d54812f344aa9e80f8f4423362af6f93, 7d96f3626ddc4d32b71c5e9af135ea0647a61985,
7e1b66f51921b5d302948cb63a33e58bf0fa1440, bbfb34352ac1fe26b1ab27cfb82a0f13069ed375,
dde8bf123776cbd89b8c9fd712de058b9ca943f7, c62ea7150642ea3b36a4a7fffe71ba6b6f65bc67,
e8b683f65a3e15d4814628f8820c1ad6997fa0aa, 3ecac9c28ce4eab8ed42b20cd11bf7eeaa168cc5,
386654a4c9873caffc4ec8ba84f13a7a55ec77f4, 3462c06e6fd9e112d772877442a20690b1245381,
102f1ad82aa932ef17799e839b82fb74b76dedfc, a167a7bf77b37cd95626dff53ba72b3c8f349b04

3. The repository has 5 pull requests (#1–#5) whose cached refs/diffs may
   still reference pre-rewrite commits; please clear those as needed as well.

We have verified that a fresh clone no longer contains any of these objects.
Thank you.

---
(After Support confirms: verify each SHA returns 404 via
`gh api repos/BranchMetrics/bizops-knowledge-base/git/blobs/<sha>`.)
