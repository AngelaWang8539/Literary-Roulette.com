/* Literary Roulette: interface translation (English / 简体中文 / Français).
 *
 * How it works: this file watches the page. Whenever visible English text matches an
 * entry in ENTRIES below, it is swapped for the chosen language. Templates and app.js
 * need no changes, and new entries can be added to ENTRIES at any time.
 *
 * Entry format: [ "English text", "简体中文", "Français" ]
 * Use {name} for the changing parts, e.g. "Made by {name}."
 */
(function () {
  "use strict";

  const STORE_KEY = "lr_ui_lang";
  const HTML_LANG = { en: "en", zh: "zh-Hans", fr: "fr" };
  // Never touch reading material or book data, only interface text.
  const SKIP = "script, style, textarea, #passage-text, .preview, .description, [translate='no']";
  const ATTRS = ["placeholder", "aria-label", "title", "alt"];

  const ENTRIES = [
    // ---- header, footer ----
    ["Skip to main content", "跳到主要内容", "Aller au contenu principal"],
    ["One spin. One passage. One unexpected discovery.", "一次转动，一段文字，一个意外的发现。", "Un tour. Un passage. Une découverte inattendue."],
    ["Home", "首页", "Accueil"],
    ["My History", "我的阅读记录", "Mon historique"],
    ["My Saved Books", "我的收藏", "Mes livres enregistrés"],
    ["About", "关于", "À propos"],
    ["Main", "主导航", "Principale"],
    ["Language", "语言", "Langue"],
    ["Choose reading language", "选择阅读语言", "Choisir la langue de lecture"],
    ["Made by {name}.", "制作者：{name}。", "Réalisé par {name}."],
    ["How it works", "运作方式", "Comment ça marche"],
    ["Content rights", "内容版权", "Droits sur les contenus"],
    ["Privacy", "隐私", "Confidentialité"],
    ["Rights and content contact: {contact}", "版权与内容联系方式：{contact}", "Contact pour les droits et contenus : {contact}"],

    // ---- roulette page ----
    ["Your browser is blocking local storage (private browsing?). History, saved books and uploads will only last until you close this tab.",
      "你的浏览器阻止了本地存储（是否处于隐私浏览模式？）。历史记录、收藏的书和上传内容只会保留到你关闭此标签页为止。",
      "Votre navigateur bloque le stockage local (navigation privée ?). L’historique, les livres enregistrés et les fichiers téléversés ne seront conservés que jusqu’à la fermeture de cet onglet."],
    ["You are spinning from your own uploaded books. They stay in this browser and are never sent to the server.",
      "你正在从自己上传的书中抽取段落。这些书只保存在此浏览器中，绝不会发送到服务器。",
      "Vous tirez au sort dans vos propres livres téléversés. Ils restent dans ce navigateur et ne sont jamais envoyés au serveur."],
    ["Read a passage with no title attached. Keep reading, find out where it comes from, or move on to a different book.",
      "阅读一段不附书名的文字。可以继续读下去、查明它出自哪本书，或者换一本新书。",
      "Lisez un passage sans titre. Continuez la lecture, découvrez d’où il vient, ou passez à un autre livre."],
    ["A passage from an unnamed book", "一段来自未具名书籍的文字", "Un passage d’un livre sans nom"],
    ["Loading your first passage…", "正在加载你的第一段文字……", "Chargement de votre premier passage…"],
    ["Loading…", "加载中……", "Chargement…"],
    ["Spin for a passage", "转一转，抽一段文字", "Tirer un passage"],
    ["What would you like to do?", "你想做什么？", "Que souhaitez-vous faire ?"],
    ["Pick any option, in any order, at any time.", "随时可以按任意顺序选择任一选项。", "Choisissez n’importe quelle option, dans l’ordre que vous voulez, à tout moment."],
    ["Another passage from this book", "同一本书的另一段", "Un autre passage de ce livre"],
    ["Keep reading the same book, still unnamed", "继续读同一本书，书名仍然保密", "Continuez la même lecture, toujours sans titre"],
    ["Reveal this book", "揭晓这本书", "Révéler ce livre"],
    ["See the title, author and where to read it", "查看书名、作者以及在哪里阅读", "Voir le titre, l’auteur et où le lire"],
    ["Spin another book", "换一本书", "Tirer un autre livre"],
    ["Start fresh with a different, hidden book", "重新开始，换一本隐藏书名的书", "Repartez avec un autre livre, de nouveau caché"],
    ["Upload my book", "上传我的书", "Téléverser mon livre"],
    ["Spin from your own .txt or .epub, kept in this browser", "从你自己的 .txt 或 .epub 中抽取，文件只保存在此浏览器", "Tirez au sort dans votre propre .txt ou .epub, conservé dans ce navigateur"],
    ["This visit", "本次访问", "Cette visite"],
    ["Passages read", "已读段落", "Passages lus"],
    ["Books revealed", "已揭晓的书", "Livres révélés"],
    ["Links opened", "打开的链接", "Liens ouverts"],

    // ---- book details ----
    ["Demonstration book: the text is fictional and written for testing. Any links shown are placeholders, not real stores.",
      "演示用书：文字为虚构，仅用于测试。所示链接均为占位符，并非真实商店。",
      "Livre de démonstration : le texte est fictif et rédigé pour les tests. Les liens affichés sont des espaces réservés, pas de vraies boutiques."],
    ["Genres", "类型", "Genres"],
    ["Where to go next", "接下来去哪里", "Pour aller plus loin"],
    // ---- reveal panel: link buttons, credit, publication, passage source ----
    ["Read online", "在线阅读", "Lire en ligne"],
    ["Borrow", "借阅", "Emprunter"],
    ["Buy e-book", "购买电子书", "Acheter l’e-book"],
    ["by {x}", "作者：{x}", "par {x}"],
    ["Text source: Project Gutenberg eBook #{n}", "文本来源：Project Gutenberg 电子书 #{n}", "Source du texte : Project Gutenberg, livre numérique n° {n}"],
    ["Text source: {x}", "文本来源：{x}", "Source du texte : {x}"],
    ["Project Gutenberg records this title as public domain in the USA. Copyright status can differ by country.",
      "Project Gutenberg 将这本书记录为在美国属于公有领域。各国的版权状况可能不同。",
      "Project Gutenberg indique que ce titre est dans le domaine public aux États-Unis. Le statut du droit d’auteur peut varier selon les pays."],
    ["Verify the source edition and local copyright rules before redistribution.",
      "再分发之前，请核实来源版本和当地的版权规定。",
      "Vérifiez l’édition source et les règles locales de droit d’auteur avant toute redistribution."],
    ["First published {y}. Publisher: {p}.", "首次出版于 {y} 年。出版社：{p}。", "Première publication en {y}. Éditeur : {p}."],
    ["First published {y}.", "首次出版于 {y} 年。", "Première publication en {y}."],
    ["Publisher: {p}.", "出版社：{p}。", "Éditeur : {p}."],
    ["This passage: Project Gutenberg eBook #{n}, Passage {k}.", "本段文字：Project Gutenberg 电子书 #{n}，第 {k} 段。", "Ce passage : Project Gutenberg, livre numérique n° {n}, passage {k}."],
    ["This passage: Project Gutenberg eBook #{n}.", "本段文字：Project Gutenberg 电子书 #{n}。", "Ce passage : Project Gutenberg, livre numérique n° {n}."],
    ["This passage: Passage {k}.", "本段文字：第 {k} 段。", "Ce passage : passage {k}."],
    ["No reading, borrowing or buying links have been added for this book yet.", "这本书还没有添加阅读、借阅或购买链接。", "Aucun lien de lecture, d’emprunt ou d’achat n’a encore été ajouté pour ce livre."],
    ["Save this book", "收藏这本书", "Enregistrer ce livre"],
    ["Saved. Remove from saved", "已收藏。点击取消收藏", "Enregistré. Retirer des enregistrés"],
    ["Hide book details", "隐藏书籍详情", "Masquer les détails du livre"],
    ["Show book details", "显示书籍详情", "Afficher les détails du livre"],
    ["Keep the passage, put the title away", "保留这段文字，把书名收起来", "Gardez le passage, cachez le titre"],
    ["Had you heard of this book before today?", "在今天之前，你听说过这本书吗？", "Aviez-vous entendu parler de ce livre avant aujourd’hui ?"],
    ["Yes", "是", "Oui"],
    ["No", "否", "Non"],
    ["Not sure", "不确定", "Pas sûr"],
    ["Unknown author", "作者未知", "Auteur inconnu"],
    ["Opens in a new tab", "在新标签页中打开", "S’ouvre dans un nouvel onglet"],
    ["Content note: {x}", "内容提示：{x}", "Avertissement de contenu : {x}"],
    ["Spoiler level: {x}", "剧透程度：{x}", "Niveau de spoilers : {x}"],
    ["This passage: {x}.", "本段文字：{x}。", "Ce passage : {x}."],

    // ---- My History ----
    ["Every passage you have seen is kept in this browser only. It does not sync to other devices and is never sent to the server. Opening a passage does not reveal its book.",
      "你看过的每一段文字都只保存在此浏览器中，不会同步到其他设备，也不会发送到服务器。打开某一段并不会揭晓它所属的书。",
      "Chaque passage que vous avez vu est conservé uniquement dans ce navigateur. Il n’est pas synchronisé avec d’autres appareils et n’est jamais envoyé au serveur. Ouvrir un passage ne révèle pas son livre."],
    ["Search", "搜索", "Rechercher"],
    ["Words from a passage", "段落中的词语", "Mots d’un passage"],
    ["Show", "显示", "Afficher"],
    ["All passages", "全部段落", "Tous les passages"],
    ["Book revealed", "已揭晓书籍", "Livre révélé"],
    ["Book not revealed", "未揭晓书籍", "Livre non révélé"],
    ["Clear all history", "清除全部记录", "Effacer tout l’historique"],
    ["Nothing here yet. Spin a passage and it will appear in your history.", "这里还没有内容。抽一段文字，它就会出现在你的阅读记录里。", "Rien ici pour l’instant. Tirez un passage et il apparaîtra dans votre historique."],
    ["Book revealed: {t} by {a}", "已揭晓：{t}，作者 {a}", "Livre révélé : {t}, par {a}"],
    ["Book revealed: {t}", "已揭晓：{t}", "Livre révélé : {t}"],
    ["(your upload)", "（你上传的）", "(votre téléversement)"],
    ["Open passage", "打开段落", "Ouvrir le passage"],
    ["Remove", "移除", "Supprimer"],
    ["{n} passages shown.", "显示 {n} 段文字。", "{n} passages affichés."],
    ["{n} passage shown.", "显示 {n} 段文字。", "{n} passage affiché."],
    ["No passages match.", "没有匹配的段落。", "Aucun passage ne correspond."],

    // ---- My Saved Books ----
    ["Books you chose to save after revealing them. Kept in this browser only.", "你在揭晓后选择收藏的书，仅保存在此浏览器中。", "Les livres que vous avez choisi d’enregistrer après les avoir révélés. Conservés uniquement dans ce navigateur."],
    ["No saved books yet. Reveal a book, then choose “Save this book”.", "还没有收藏的书。先揭晓一本书，再选择“收藏这本书”。", "Aucun livre enregistré pour l’instant. Révélez un livre, puis choisissez « Enregistrer ce livre »."],
    ["Remove from saved", "取消收藏", "Retirer des enregistrés"],

    // ---- Upload page ----
    ["Private by design.", "隐私优先的设计。", "Confidentialité dès la conception."],
    ["Your book is opened and stored inside this browser only (in IndexedDB). It is never uploaded to the server and never shown to other visitors. It will not follow you to another device or browser.",
      "你的书只会在此浏览器内打开并存储（使用 IndexedDB）。它绝不会上传到服务器，也不会展示给其他访客，更不会跟随你到其他设备或浏览器。",
      "Votre livre est ouvert et stocké uniquement dans ce navigateur (dans IndexedDB). Il n’est jamais téléversé sur le serveur ni montré aux autres visiteurs. Il ne vous suivra pas sur un autre appareil ou navigateur."],
    // The next three are fragments of one sentence, split by the <code> tags in the template.
    ["Supported: plain text (", "支持：纯文本（", "Formats acceptés : texte brut ("],
    [") and", "）和", ") et"],
    ["files up to 15 MB. Books protected by DRM cannot be opened, and Literary Roulette does not try to remove DRM. Only upload books you are entitled to use.",
      "文件，大小不超过 15 MB。受 DRM 保护的书无法打开，Literary Roulette 也不会尝试破除 DRM。请只上传你有权使用的书。",
      "jusqu’à 15 Mo. Les livres protégés par DRM ne peuvent pas être ouverts, et Literary Roulette n’essaie pas de supprimer les DRM. Ne téléversez que des livres que vous avez le droit d’utiliser."],
    ["Choose a .txt or .epub file", "选择一个 .txt 或 .epub 文件", "Choisissez un fichier .txt ou .epub"],
    ["Spin passages from", "抽取段落的来源", "Tirer des passages depuis"],
    ["The public catalogue", "公共书库", "Le catalogue public"],
    ["My uploaded books", "我上传的书", "Mes livres téléversés"],
    ["Your uploaded books", "你上传的书", "Vos livres téléversés"],
    ["No uploaded books yet.", "还没有上传的书。", "Aucun livre téléversé pour l’instant."],
    ["Remove this book and its local data", "移除这本书及其本地数据", "Supprimer ce livre et ses données locales"],
    ["Your upload. Stored only in this browser.", "你上传的书，仅保存在此浏览器中。", "Votre téléversement. Conservé uniquement dans ce navigateur."],
    ["Uploaded from your device ({file}). Stored only in this browser.", "从你的设备上传（{file}），仅保存在此浏览器中。", "Téléversé depuis votre appareil ({file}). Conservé uniquement dans ce navigateur."],
    ["Reading your book on this device…", "正在你的设备上读取这本书……", "Lecture de votre livre sur cet appareil…"],
    ["Added “{t}” with about {n} passages. It is stored only in this browser.", "已添加《{t}》，约 {n} 段文字。它只保存在此浏览器中。", "« {t} » ajouté, avec environ {n} passages. Il est conservé uniquement dans ce navigateur."],
    ["Not enough readable story text was found in this file.", "此文件中没有找到足够的可读正文。", "Pas assez de texte narratif lisible n’a été trouvé dans ce fichier."],
    ["Something went wrong while reading this file. It may be damaged or too large for your browser.", "读取此文件时出错。文件可能已损坏，或对你的浏览器来说太大。", "Une erreur s’est produite lors de la lecture de ce fichier. Il est peut-être endommagé ou trop volumineux pour votre navigateur."],
    ["Book removed from this browser.", "已从此浏览器中移除这本书。", "Livre supprimé de ce navigateur."],
    ["That uploaded book has been removed.", "那本上传的书已被移除。", "Ce livre téléversé a été supprimé."],

    // ---- status messages ----
    ["Your passage is ready. Choose any option.", "你的段落准备好了。请选择任一选项。", "Votre passage est prêt. Choisissez une option."],
    ["New passage from the same book.", "同一本书的新段落。", "Nouveau passage du même livre."],
    ["New passage from the same book. Book details are still showing.", "同一本书的新段落。书籍详情仍在显示。", "Nouveau passage du même livre. Les détails du livre sont toujours affichés."],
    ["A different book, hidden again. Read on.", "换了一本书，书名再次隐藏。继续读吧。", "Un autre livre, de nouveau caché. Poursuivez la lecture."],
    ["Book revealed. Your passage has not changed.", "书已揭晓，你的段落没有变化。", "Livre révélé. Votre passage n’a pas changé."],
    ["Book details hidden. Your passage has not changed.", "书籍详情已隐藏，你的段落没有变化。", "Détails du livre masqués. Votre passage n’a pas changé."],
    ["Thanks for answering.", "谢谢你的回答。", "Merci de votre réponse."],
    ["Removed from My Saved Books.", "已从“我的收藏”中移除。", "Retiré de Mes livres enregistrés."],
    ["Saved to My Saved Books on this device.", "已保存到此设备上的“我的收藏”。", "Enregistré dans Mes livres enregistrés sur cet appareil."],
    ["Passage reopened from your history. Its book is still hidden unless you reveal it.", "已从阅读记录中重新打开该段落。除非你揭晓，否则它所属的书仍然隐藏。", "Passage rouvert depuis votre historique. Son livre reste caché tant que vous ne le révélez pas."],
    ["Passage loaded from your uploaded books.", "已从你上传的书中载入段落。", "Passage chargé depuis vos livres téléversés."],
    ["You have not uploaded any books yet.", "你还没有上传任何书。", "Vous n’avez encore téléversé aucun livre."],
    ["This book has only one passage, so there is no other to show. You can still reveal it or spin another book.", "这本书只有一段文字，没有其他可显示的了。你仍然可以揭晓它，或换一本书。", "Ce livre n’a qu’un seul passage, il n’y en a donc pas d’autre à montrer. Vous pouvez quand même le révéler ou tirer un autre livre."],
    ["You have only uploaded one book, so there is nothing else to spin to.", "你只上传了一本书，没有其他可换的了。", "Vous n’avez téléversé qu’un seul livre, il n’y a donc rien d’autre à tirer."],
    ["You have now met every uploaded book once. Starting a fresh round.", "你已经遇到过每一本上传的书了。重新开始新一轮。", "Vous avez maintenant croisé chacun de vos livres téléversés une fois. Nouveau tour."],
    ["Could not reach the site. Check your connection and try again.", "无法连接到网站。请检查网络连接后重试。", "Impossible de joindre le site. Vérifiez votre connexion et réessayez."],
    ["Something went wrong. Please try again.", "出错了，请重试。", "Une erreur s’est produite. Veuillez réessayer."],

    // ---- messages sent by the server (app.py) ----
    ["That passage is no longer available. Spin for a new one.", "该段落已不可用。请转一次获取新的段落。", "Ce passage n’est plus disponible. Tirez-en un nouveau."],
    ["This book has no other approved passage yet. You can still reveal it or spin another book.", "这本书暂时没有其他已批准的段落。你仍然可以揭晓它，或换一本书。", "Ce livre n’a pas encore d’autre passage approuvé. Vous pouvez quand même le révéler ou tirer un autre livre."],
    ["There is only one book in the catalogue right now, so there is nothing else to spin to.", "目前书库里只有一本书，没有其他可换的了。", "Il n’y a qu’un seul livre dans le catalogue pour le moment, il n’y a donc rien d’autre à tirer."],
    ["You have now met every book once. Starting a fresh round.", "你已经遇到过每一本书了。重新开始新一轮。", "Vous avez maintenant croisé chaque livre une fois. Nouveau tour."],

    // ---- confirmation dialogs ----
    ["Are you sure?", "确定吗？", "Êtes-vous sûr ?"],
    ["Cancel", "取消", "Annuler"],
    ["Confirm", "确认", "Confirmer"],
    ["Clear your reading history?", "清除你的阅读记录？", "Effacer votre historique de lecture ?"],
    ["Every passage you have seen will be removed from this browser. This cannot be undone.", "你看过的所有段落都将从此浏览器中删除，此操作无法撤销。", "Tous les passages que vous avez vus seront supprimés de ce navigateur. Cette action est irréversible."],
    ["Clear history", "清除记录", "Effacer l’historique"],
    ["Remove this book?", "移除这本书？", "Supprimer ce livre ?"],
    ["This removes the book and everything stored about it in this browser, including its history and saved entry.", "这将移除这本书以及此浏览器中与它相关的所有数据，包括阅读记录和收藏条目。", "Cela supprime le livre et tout ce qui le concerne dans ce navigateur, y compris son historique et son enregistrement."],
    ["Remove book", "移除书籍", "Supprimer le livre"],
    // the browser-style pop-ups, in case they are still in use
    ["Clear your whole reading history on this device? This cannot be undone.", "要清除此设备上的全部阅读记录吗？此操作无法撤销。", "Effacer tout votre historique de lecture sur cet appareil ? Cette action est irréversible."],
    ["Remove this book and everything stored about it in this browser, including its history and saved entry?", "要移除这本书以及此浏览器中与它相关的所有数据（包括阅读记录和收藏条目）吗？", "Supprimer ce livre et tout ce qui le concerne dans ce navigateur, y compris son historique et son enregistrement ?"],
  ];

  // ---------- compile the table ----------
  const norm = (s) => s.trim().replace(/\s+/g, " ");

  // Common Project Gutenberg bookshelf names shown as genre tags: [English, 简体中文, Français].
  const SHELVES = [
    ["Poetry", "诗歌", "Poésie"], ["Novels", "小说", "Romans"], ["Fiction", "小说", "Fiction"],
    ["Science Fiction", "科幻小说", "Science-fiction"], ["Fantasy", "奇幻", "Fantastique"],
    ["Adventure", "冒险", "Aventure"], ["Mystery Fiction", "悬疑小说", "Roman policier"],
    ["Detective Fiction", "侦探小说", "Roman policier"], ["Crime Fiction", "犯罪小说", "Roman noir"],
    ["Horror", "恐怖", "Horreur"], ["Gothic Fiction", "哥特小说", "Roman gothique"],
    ["Romance", "爱情小说", "Romance"], ["Historical Fiction", "历史小说", "Roman historique"],
    ["Humor", "幽默", "Humour"], ["Humour", "幽默", "Humour"], ["Satire", "讽刺文学", "Satire"],
    ["Children's Literature", "儿童文学", "Littérature jeunesse"], ["Children's Fiction", "儿童小说", "Fiction pour la jeunesse"],
    ["Plays/Films/Dramas", "戏剧／电影", "Théâtre et cinéma"], ["Drama", "戏剧", "Théâtre"],
    ["Short Stories", "短篇小说", "Nouvelles"], ["Essays", "散文随笔", "Essais"],
    ["Philosophy", "哲学", "Philosophie"], ["Biographies", "传记", "Biographies"], ["Autobiographies", "自传", "Autobiographies"],
    ["History", "历史", "Histoire"], ["Politics", "政治", "Politique"], ["Religion", "宗教", "Religion"],
    ["Science", "科学", "Sciences"], ["Mathematics", "数学", "Mathématiques"], ["Travel", "旅行", "Voyages"], ["War", "战争", "Guerre"],
    ["Fairy Tales", "童话", "Contes de fées"], ["Mythology", "神话", "Mythologie"], ["Folklore", "民间传说", "Folklore"],
    ["Classics of Literature", "文学经典", "Classiques de la littérature"], ["Western Literature", "西方文学", "Littérature occidentale"],
    ["Best Books Ever Listings", "史上最佳书单", "Les meilleurs livres de tous les temps"], ["Harvard Classics", "哈佛经典", "Harvard Classics"],
    ["Literature", "文学", "Littérature"], ["American Literature", "美国文学", "Littérature américaine"],
    ["British Literature", "英国文学", "Littérature britannique"], ["English Literature", "英国文学", "Littérature anglaise"],
    ["French Literature", "法国文学", "Littérature française"], ["German Literature", "德国文学", "Littérature allemande"],
    ["Russian Literature", "俄国文学", "Littérature russe"], ["Chinese Literature", "中国文学", "Littérature chinoise"],
    ["FR Littérature", "法语文学", "FR Littérature"], ["FR Poésie", "法语诗歌", "FR Poésie"],
    ["FR Théâtre", "法语戏剧", "FR Théâtre"], ["FR Histoire", "法语历史", "FR Histoire"],
  ];
  for (const [en, zh, fr] of SHELVES) {
    ENTRIES.push(["Category: " + en, "类别：" + zh, "Catégorie : " + fr]);
    ENTRIES.push([en, zh, fr]);
  }
  ENTRIES.push(["Category: {x}", "类别：{x}", "Catégorie : {x}"]); // any other shelf keeps its own name

  const exact = { zh: new Map(), fr: new Map() };
  const patterns = { zh: [], fr: [] };
  for (const [en, zh, fr] of ENTRIES) {
    for (const [code, out] of [["zh", zh], ["fr", fr]]) {
      if (en.includes("{")) {
        const names = [];
        const body = en.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
          .replace(/\\\{(\w+)\\\}/g, (_, k) => { names.push(k); return "(.+?)"; });
        patterns[code].push({ re: new RegExp("^" + body + "$"), names, out });
      } else {
        exact[code].set(norm(en), out);
      }
    }
  }

  function translate(text, lang) {
    if (lang === "en" || !text) return text;
    const core = norm(text);
    if (!core) return text;
    const lead = text.match(/^\s*/)[0], trail = text.match(/\s*$/)[0];
    const hit = exact[lang].get(core);
    if (hit !== undefined) return lead + hit + trail;
    for (const p of patterns[lang]) {
      const m = core.match(p.re);
      if (m) return lead + p.out.replace(/\{(\w+)\}/g, (_, k) => m[p.names.indexOf(k) + 1] || "") + trail;
    }
    return text;
  }

  // Export for testing outside the browser, then stop.
  if (typeof document === "undefined") {
    if (typeof module !== "undefined") module.exports = { translate, ENTRIES };
    return;
  }

  // ---------- apply to the page ----------
  let lang = "en";
  const textRec = new WeakMap(); // Text node -> { orig, shown }
  const attrRec = new WeakMap(); // Element -> { attrName: { orig, shown } }

  function doText(node) {
    const now = node.nodeValue;
    const parent = node.parentElement;
    if (!parent || !now || !now.trim() || parent.closest(SKIP)) return;
    let rec = textRec.get(node);
    if (!rec || now !== rec.shown) rec = { orig: now, shown: now }; // new English text from the app
    const out = translate(rec.orig, lang);
    if (out !== now) node.nodeValue = out;
    rec.shown = out;
    textRec.set(node, rec);
  }

  function doAttrs(el) {
    let recs = attrRec.get(el);
    for (const a of ATTRS) {
      if (!el.hasAttribute(a)) continue;
      recs = recs || {};
      const now = el.getAttribute(a);
      let r = recs[a];
      if (!r || now !== r.shown) r = { orig: now, shown: now };
      const out = translate(r.orig, lang);
      if (out !== now) el.setAttribute(a, out);
      r.shown = out;
      recs[a] = r;
    }
    if (recs) attrRec.set(el, recs);
  }

  function walk(root) {
    if (root.nodeType === 3) return doText(root);
    if (root.nodeType !== 1) return;
    doAttrs(root);
    root.querySelectorAll(ATTRS.map((a) => "[" + a + "]").join(",")).forEach(doAttrs);
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = tw.nextNode())) doText(n);
  }

  function setLang(code, persist) {
    if (!HTML_LANG[code]) code = "en";
    lang = code;
    document.documentElement.lang = HTML_LANG[code];
    if (persist !== false) { try { localStorage.setItem(STORE_KEY, code); } catch (e) { /* private mode */ } }
    walk(document.body);
    document.dispatchEvent(new CustomEvent("lr:langchange", { detail: { lang: code } }));
  }

  // ---------- follow the language selector ----------
  function codeFromSelect(sel) {
    const opt = sel.selectedOptions && sel.selectedOptions[0];
    const probe = (sel.value + " " + (opt ? opt.textContent : "")).toLowerCase();
    if (/zh|中文|chinese|hans|汉/.test(probe)) return "zh";
    if (/fr|fran[cç]ais|french/.test(probe)) return "fr";
    return "en";
  }

  function syncWithSelect() {
    const sel = document.querySelector(".language-switch select, label[aria-label='Choose reading language'] select");
    if (!sel) return;
    if (!sel.dataset.lrBound) {
      sel.dataset.lrBound = "1";
      sel.addEventListener("change", () => setLang(codeFromSelect(sel)));
    }
    const code = codeFromSelect(sel);
    if (code !== lang) setLang(code);
  }

  // ---------- start ----------
  const observer = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === "characterData") doText(m.target);
      else if (m.type === "attributes") doAttrs(m.target);
      else m.addedNodes.forEach(walk);
    }
    syncWithSelect();
  });
  observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  setInterval(syncWithSelect, 1000); // catches the selector being changed by code, without a change event

  // Browser pop-ups (confirm/alert) get translated too.
  for (const name of ["confirm", "alert"]) {
    const original = window[name].bind(window);
    window[name] = (msg) => original(translate(String(msg), lang));
  }

  window.LR_I18N = { t: (s) => translate(s, lang), setLang, getLang: () => lang };

  let saved = "en";
  try { saved = localStorage.getItem(STORE_KEY) || "en"; } catch (e) { /* ignore */ }
  setLang(saved, false);
  syncWithSelect();
})();
