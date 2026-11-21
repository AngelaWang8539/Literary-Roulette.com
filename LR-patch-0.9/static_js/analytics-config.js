// PostHog settings. Leave `key` empty and the site sends nothing to anyone.
// The project API key is public by design (it only lets a browser SEND events), so it is fine to commit.
window.LR_ANALYTICS = {
  key: "",                                  // paste your PostHog project API key here (usually starts with phc_)
  host: "https://us.i.posthog.com",         // an EU PostHog project uses https://eu.i.posthog.com
  persist: "memory",                        // "memory": no cookie, no localStorage, so no consent banner needed for it.
                                            // Later, for real unique-visitor counts: "localStorage+cookie" (add consent + Privacy text first)
};
