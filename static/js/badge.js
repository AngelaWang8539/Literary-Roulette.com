// Concise popularity badge: turns the server's popularity data into a short pill ("Top 1%") and a longer line for the share card.
// Pure function, no DOM: add a new tier or wording here and nowhere else. Data comes from popularity.py / universe.py (unchanged).
export function badgeText(p) {
  if (!p || !p.label) return null;
  const pct = Number(p.top_percent) > 0 ? Math.round(Number(p.top_percent)) : 0;
  switch (p.tier) {
    case "jackpot":
      return pct ? { short: `Top ${pct}%`, long: `Top ${pct}% most popular` } : { short: "Top 10 this month", long: "Top 10 most popular this month" };
    case "lucky":
      return pct ? { short: `Top ${pct}%`, long: `Top ${pct}% most popular` } : { short: "Top 100 this month", long: "Top 100 most popular this month" };
    case "hidden-gem":
      return pct >= 91 || (!pct && p.rank) ? { short: "Last 10%", long: "Among the least read 10%" } : { short: "Off the list", long: "Off the beaten shelf" };
    case "offlist":      // neutral: only says "not in the top list"; never a claim about being unpopular
      return { short: "Off the list", long: "Off the beaten shelf" };
    default:
      return { short: "Discovery", long: "A fresh discovery" };
  }
}
