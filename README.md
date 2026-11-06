# eMAG AutoBuy

Aplicație **locală** pentru monitorizarea produselor eMAG și pregătirea unui coș comun. Folosește Python 3.12+, Playwright, Streamlit, SQLite și PyYAML; este gândită pentru Windows 11.

**Fluxul eMAG verificat este doar pentru România (`emag.ro`, RON, Europe/Bucharest).** Aplicația recunoaște și setările de piață pentru Ungaria (`emag.hu`, HUF, Europe/Budapest), dar oprește pornirea monitorizării până când pagina reală de produs și checkout pot fi inspectate. Linkul HU trimis în această sesiune a redirecționat de la 404 la pagina principală, apoi site-ul a afișat o verificare anti-bot. Nu sunt activați selectori inventați și nu se ocolește verificarea. Vezi [`docs/MULTI_MARKET_PLAN.md`](docs/MULTI_MARKET_PLAN.md). Pagina Vercel descrie aceeași limită.

Codul este public sub licența MIT. Proiectul Vercel inclus în acest repository publică doar pagina informativă statică din `public/`; nu găzduiește dashboardul, profilul Playwright, baza SQLite sau fluxul de cumpărare. Pornește aplicația pe calculatorul tău urmând pașii de mai jos.

Pagina publică: [emag-autobuy.vercel.app](https://emag-autobuy.vercel.app).

Copiază `config.example.yaml` ca `config.yaml` pentru un exemplu neutru sau pornește fără configurare: aplicația generează automat un fișier local gol. `config.yaml`, profilul browserului, baza de date și jurnalele nu se publică.

## Reguli pentru comenzi

- Modul Test este bifat implicit și nu schimbă coșul, checkout-ul sau comanda.
- Fiecare produs are o bifă explicită „Este bilet digital”; trebuie activată doar pentru un bilet pe care vrei să îl cumperi.
- Fluxul live poate pregăti până la 10 produse într-un singur coș comun. Toate trebuie să îndeplinească propriile condiții înainte de adăugare; dacă unul expiră sau nu se califică, coșul nu se pregătește.
- Codul poate apăsa o singură dată butonul final „Trimite comanda” numai dacă fiecare produs a fost bifat explicit ca bilet digital, checkout-ul confirmă destinatarul fără ambiguitate, iar totalul complet afișat pentru întregul coș este între 0 și 100,00 RON inclusiv.
- Totalul trebuie să includă subtotalul, livrarea, serviciile, taxele și reducerile afișate, iar liniile trebuie să se reconcilieze exact cu suma de plată. Orice valoare, produs, cantitate sau pas de confirmare ambiguu oprește fluxul.
- Nu se reîncearcă un click cu rezultat incert. Verifică manual istoricul comenzilor eMAG înainte de orice reluare.
- Când confirmarea digitală, totalul sau butonul final nu pot fi verificate, browserul rămâne deschis pentru verificare manuală. Aplicația nu apasă butonul în această rezervă; după verificare, apasă tu butonul eMAG și apoi „Oprește” în dashboard.
- Înainte de adăugare, coșul trebuie să fie confirmat gol. După fiecare adăugare, aplicația verifică rândurile, cantitățile și subtotalul cumulat; produse suplimentare sau rânduri neclare opresc trimiterea.
- Un coș lăsat pentru verificare manuală este marcat în SQLite și nu poate fi relansat automat cu aceeași listă, orare și cantități. Verifică istoricul eMAG înainte să schimbi orarul și să încerci din nou.
- CAPTCHA și 3-D Secure rămân manuale. Aplicația nu citește ori stochează parole, OTP-uri, adrese de email, numere de card, CVV sau credențiale bancare.

Dezactivarea Modului Test poate trimite o comandă reală dacă toate condițiile de mai sus sunt îndeplinite. Nu folosi un URL live până când ai verificat personal produsul, cantitatea, destinatarul și totalul.

## Limită de verificare live

Site-ul eMAG a fost raportat indisponibil până la ora 08:00, așa că modificările curente au fost verificate doar cu pagini HTML locale Playwright și servicii mock-uite. Fluxul real al coșului cu mai multe rânduri, al biletelor digitale, selectorul semantic al butonului intermediar, starea de succes după confirmarea adresei și pagina reală de confirmare a comenzii nu au putut fi validate în această versiune.

În inspecții anterioare, pagina de produs a expus titlul prin `h1.page-title`, prețul curent în `[data-test="main-price"]`, stocul în eticheta de stoc și controlul de adăugare în coș prin `button[data-test="main-add-to-cart-button"]`. Prețul PRP tăiat era separat; extractorul refuză mai multe sume în același câmp.

Coșul și checkout-ul populate au fost observate anterior în arborele de accesibilitate al Chrome, nu prin DOM Playwright. S-au văzut controale accesibile „Cantitate”, „Scade cantitatea”, „Crește cantitatea” și liniile „Cost produse”, „Cost livrare”, „Servicii operationale” și „Total”. Un exemplu a avut 16,37 RON în coș și 17,36 RON la checkout după taxa de 0,99 RON. Aceste observații nu validează selectorii ori structura live de azi.

**Nu este declarată production-ready.** După ce site-ul revine, folosește mai întâi Mod Test și verifică manual DOM-ul real cu Playwright. Nu dezactiva modul Test până când pagina reală de bilet, coșul cu mai multe rânduri, confirmarea destinatarului și toate taxele din checkout au fost inspectate. Nu am plasat nicio comandă reală.

## Arhitectură

```text
Streamlit dashboard (app.py)
    └─ WorkerController (core/runtime.py)
        ├─ ceas NTP și orare Europe/Bucharest (core/scheduler.py)
        ├─ monitorizare multi-produs, navigare serializată (core/multi_monitor.py)
        └─ BrowserManager + EmagPage (browser/)
            └─ istoric de evenimente și protecție anti-duplicat (database/db.py)
```

Produsele sunt configurate în `config.yaml`; profilul browserului persistent și baza SQLite sunt în `data/`.

## Instalare pe Windows 11

În PowerShell, deschide folderul `emag-autobuy` și rulează:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

Dacă PowerShell blochează activarea mediului:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Pornire

```powershell
.\.venv\Scripts\python.exe main.py
```

`main.py` deschide automat dashboardul în browserul local la `http://127.0.0.1:8501`; aplicația ascultă doar local. Când apeși **Pornește monitorizarea**, Playwright deschide imediat o fereastră vizibilă și apoi așteaptă ora programată. Pentru Ungaria există și **Deschide pagina HU acum**, care doar navighează la URL-ul introdus, fără să citească sau să apese elemente.

1. Adaugă URL-ul eMAG și setează produsul, cantitatea, plafonul unitar și ora Europe/Bucharest. Bifează explicit „Este bilet digital” numai pentru bilete.
2. Apasă **Salvează lista de produse**.
3. Lasă **Mod Test** bifat pentru prima verificare. Testul nu adaugă produsul în coș.
4. Profilul persistent Playwright se deschide vizibil; autentifică-te manual în el dacă eMAG cere login. CAPTCHA și 3-D Secure sunt pași manuali.
5. Fluxul live comun poate continua doar dacă fiecare produs se califică, site-ul arată confirmarea destinatarului biletului digital și suma totală de checkout este cel mult 100 RON. Altfel browserul rămâne deschis pentru verificare manuală.

## Testare locală

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testele folosesc fixture-uri locale Playwright, ceasuri simulate și DB/browser mock-uite. Ele verifică parserul de preț, starea stocului, cantitatea, totalul cu taxe, pragul de 100 RON, confirmarea destinatarului, butonul final unic, protecția anti-duplicat și programarea. **Nu verifică eMAG live și nu demonstrează că pasul real pentru bilete funcționează.**

## Profil Chrome și endpoint CDP

Pentru utilizarea obișnuită, alege **Profil Playwright separat** și autentifică-te manual o singură dată în fereastra persistentă a aplicației. Profilul este stocat local.

Modul **Chrome separat, pornit manual cu endpoint CDP local** funcționează doar cu o instanță Chrome pornită cu `--remote-debugging-port=9222` și un director de profil dedicat. De exemplu, în PowerShell, după ce verifici calea instalării Chrome:

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:LOCALAPPDATA\eMAG-AutoBuy-Chrome"
```

Asta creează un profil Chrome separat în care te autentifici manual; nu folosește profilul principal deja autentificat. Chrome 136+ ignoră `--remote-debugging-port` pentru directorul implicit de date Chrome. Opțiunea din Chrome 144+ la `chrome://inspect/#remote-debugging` aparține fluxului Chrome DevTools MCP; aplicația Python de aici nu se conectează prin acel protocol. [Documentația Chrome despre conectarea automată](https://developer.chrome.com/docs/devtools/agents/get-started/configuration) și [schimbarea de securitate din Chrome 136](https://developer.chrome.com/blog/remote-debugging-port).

## Fișiere principale

- `app.py`: dashboard și lista produselor
- `main.py`: pornirea Streamlit pe localhost
- `config.yaml`: configurație
- `browser/manager.py`: profil Playwright persistent
- `browser/open_page.py`: deschidere manuală, vizibilă, a unui URL HU
- `browser/emag.py`: citire pagină, coș, ticket email și checkout vizibile
- `core/checkout.py`: reguli de verificare și pragul de 100 RON
- `core/runtime.py`: scheduler și fluxul workerului
- `core/conditions.py`: parsare preț, cantitate, stoc și total
- `database/db.py`: istoric SQLite și blocarea dublării comenzilor
- `tests/`: teste locale și mock-uri, fără tranzacții reale

## Publicare pe Vercel

Vercel servește numai pagina statică din `public/`. Aplicația de cumpărare nu este un serviciu public și nu trimite date de cont, coș sau browser către Vercel. Pagina statică nu conține formular de comandă și nu poate plasa achiziții.

Pagina curentă a fost publicată cu Vercel CLI, presetarea **Other** și folderul de ieșire `public`. Fișierul `.vercelignore` permite doar `public/index.html` și `vercel.json`; setările, profilul, baza, testele și codul workerului rămân în repository-ul GitHub, nu în deployment. GitHub nu este legat pentru redeploy automat; după schimbări în pagina statică, autentifică-te în Vercel CLI și rulează `npx vercel deploy --prod`. Aceasta nu instalează și nu pornește aplicația Python.

## Licență

Distribuit sub licența MIT. Vezi `LICENSE`.
