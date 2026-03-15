*Short Write-Up*

My goal was to build a simple bid engine that decides which sections to bid on, what price to bid, and how much capital to allocate while trying to maximize risk-adjusted profit.
First, the script normalizes section names so listings, sales data, primary availability, and bids can be matched correctly even if they use slightly different names. Then it classifies each event as strong, neutral, or weak using signals like daily sales volume, days to event, primary ticket availability, and whether competitors are already bidding.

For each section, I estimate a realistic resale price using a weighted combination of:
1. section ATP (average transaction price)
2. the current lowest ask
3. event-level ATP

Section ATP is the most important signal because it represents actual sale prices, not just seller listings. The estimate is then adjusted downward if there are negative signals like high primary availability, low sales volume, large inventory, or long time until the event.
The engine places a bid only if the section passes several checks:
1. minimum expected margin
2. minimum daily sales volume
3. minimum turnover
4. acceptable risk score

Capital allocation also considers liquidity. Sections with higher daily sales volume receive more capital because those tickets are easier to resell before the event. I also limit position size based on market capacity so the engine does not buy more tickets than the market can realistically absorb.
The engine intentionally does not force deployment of all $50,000. If the remaining sections do not meet the risk and liquidity requirements, the capital stays undeployed.
AI helped speed up the coding and structure of the engine. However, I adjusted the logic to make it more realistic for a trading system by tightening the filters, prioritizing liquidity, and preventing the model from allocating capital to weak sections.
And I corrected AI when it was having my model use up all the Capital as I saw that it should be able to see if an opportunity is not STRONG ENOUGH, and seeing the opportunities where the capital can be deployed and exited quickly.

For a production system, I would want more data such as historical price trends, bid fill rates, row-level pricing differences, and time-series inventory data. That would allow the model to better estimate future resale prices and selling probability.
