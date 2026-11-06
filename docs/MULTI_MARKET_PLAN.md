# Suportul eMAG Ungaria

## Starea codului

- Fluxul de produs/coș verificat rămâne numai pentru România.
- Configurația poate identifica `emag.hu`, moneda HUF și fusul `Europe/Budapest`; plafonul introdus este interpretat în moneda pieței detectate.
- Validarea cere un URL canonic de produs cu `/pd/` și cod de produs. URL-ul HU primit (`/R944240_1`) a afișat pagina eMAG „404 / mutată sau inexistentă” și apoi a fost redirecționat la pagina principală.
- Workerul refuză să pornească pentru HU înainte de orice acțiune în browser. Nu se reutilizează selectorii românești și nu se încearcă adăugarea în coș.

## Inspecție live din 6 noiembrie 2026

Pagina a fost deschisă în browser și DOM-ul/accesibilitatea au fost citite cu Playwright. Linkul furnizat nu a ajuns la un produs. După redirecționare și respingerea cookie-urilor, eMAG.hu a afișat un mesaj că traficul de rețea este neobișnuit de mare. Inspecția s-a oprit acolo; CAPTCHA sau altă protecție anti-bot nu a fost rezolvată ori ocolită.

Aplicația CyberGhost instalată cere confirmarea emailului contului înainte să poată conecta VPN-ul; nu s-au introdus credențiale și nu s-a schimbat ruta rețelei. Prin urmare, pagina de produs și coșul HU nu au putut fi verificate.

## Ce lipsește înainte de activarea HU

1. Un link HU curent care deschide efectiv pagina biletului în browserul utilizatorului.
2. Inspecție Playwright a produsului: titlu, variantă, prețul curent separat de orice preț tăiat, disponibilitate, cantitate și butonul vizibil.
3. Inspecție fără cumpărare a coșului și checkoutului: liniile produselor, cantități, subtotal, taxe, livrare, total HUF și pașii de confirmare ai biletului digital.
4. Fixture-uri bazate pe DOM-ul observat și teste care demonstrează citirea corectă a prețului curent și blocarea valorilor ambigue.
5. Rulare în Test Mode pe paginile reale, fără click pe butonul de comandă.

Nu se folosește un selector „probabil” și nu se transformă suma în RON fără un plafon agregat și curs verificabil. CAPTCHA și verificările de securitate rămân manuale.

## Profilul Chrome existent

`http://127.0.0.1:9222` este un endpoint CDP pentru Chrome pornit separat cu un profil dedicat. În Chrome 136+, portul de depanare nu funcționează pe directorul implicit al profilului principal. Opțiunea Chrome 144+ de la `chrome://inspect/#remote-debugging` este pentru conectarea Chrome DevTools MCP și nu este implementată de aplicația Python/Playwright. Pentru utilizare imediată, folosește profilul Playwright persistent al aplicației și autentifică-te manual acolo. Vezi [documentația oficială Chrome](https://developer.chrome.com/docs/devtools/agents/get-started/configuration) și [schimbarea de securitate din Chrome 136](https://developer.chrome.com/blog/remote-debugging-port).

## Vercel

Vercel servește doar pagina statică de prezentare. Dashboardul, profilul browserului și orice coș rămân locale pe calculator.
