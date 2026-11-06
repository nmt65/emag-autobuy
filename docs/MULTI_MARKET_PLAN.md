# Plan pentru eMAG România, Bulgaria și Ungaria

## Situația versiunii publicate

Aplicația curentă acceptă numai `emag.ro`, prețuri RON, textele românești de disponibilitate și selectorii vizibili observați pentru România. `emag.bg` și `emag.hu` sunt respinse intenționat; nu sunt încă testate sau implementate.

## Cum trebuie să funcționeze extinderea

- Un model de piață explicit pentru fiecare produs: domeniu permis, limbă, valută, fus orar și adaptor DOM.
- Un adaptor separat pentru fiecare storefront. Niciun selector sau text de stoc nu se reutilizează între țări până nu este observat direct în Playwright pe pagina reală.
- Un singur controller conectat la sesiunea Chrome autorizată, cu câte o filă de automatizare nouă pentru fiecare piață. Orchestratorul trebuie să programeze verificările fără a partaja o filă între taskuri. O eroare, autentificare expirată ori CAPTCHA pe un site nu oprește celelalte piețe.
- Coșurile și checkouturile se tratează separat până când se verifică live o funcție eMAG de coș transfrontalier. Proiectul nu va pretinde că poate crea o singură comandă între țări; în designul curent ar putea rezulta până la trei comenzi independente.
- Utilizatorul se autentifică manual în fiecare storefront necesar, în propriul profil Chrome. Nu se copiază cookie-uri și nu se exportă storage state. Dacă un storefront necesită o sesiune separată, se configurează un profil distinct fără să se copieze sesiunea existentă.
- Deduplicare și jurnalizare cu cheia `market + product_code + schedule + quantity`, ca o comandă de pe un site să nu blocheze sau să dubleze comanda altei piețe.

## Valută, buget și oră

| Piață | Monedă pentru comparații | Fus orar de programare |
| --- | --- | --- |
| România | RON | `Europe/Bucharest` |
| Bulgaria | EUR | `Europe/Sofia` |
| Ungaria | HUF | `Europe/Budapest` |

Bulgaria a adoptat euro la 1 ianuarie 2026; perioada legală de afișare duală lev/euro s-a încheiat la 8 august 2026. Ungaria folosește forintul. Aceste valori trebuie configurate explicit pe fiecare produs și validate față de suma nativă afișată de site; aplicația nu trebuie să trateze 100 RON ca 100 EUR sau 100 HUF.

Pentru un plafon global de cheltuieli între țări, implementarea trebuie să rezerve bugetul agregat înainte de checkout, să folosească un curs public verificabil și să includă un tampon pentru conversie/comisioane. Dacă nu există un curs suficient de proaspăt sau taxele nu sunt complete, comenzile automate se opresc. Alternativa mai simplă este un plafon maxim distinct, introdus de utilizator în moneda fiecărei piețe.

Surse valutare oficiale: [Comisia Europeană — Bulgaria și euro](https://economy-finance.ec.europa.eu/euro/eu-countries-and-euro/bulgaria-and-euro_en), [Uniunea Europeană — Ungaria](https://european-union.europa.eu/principles-countries-history/eu-countries/hungary_en).

## Ordinea implementării și validării

1. Înregistrează URL-uri reale de produs pentru `emag.bg` și `emag.hu`; confirmă redirecționările și domeniul final.
2. Inspectează în Playwright, fără cumpărare: titlu, preț curent (separat de prețul vechi), stoc, variantă, cantitate și butonul de adăugare.
3. Inspectează coșul și checkout-ul fiecărei piețe pentru rânduri, cantități, subtotal, toate taxele, livrare, total și pașii de plată.
4. Creează fixture-uri distincte pentru limbă și formatarea monedei; testele resping prețuri cu mai multe valori, monedă neașteptată, total lipsă și stoc neclar.
5. Rulează monitorizarea celor trei piețe simultan în Test Mode și verifică izolare, fusuri orare, oprire, reconectare și deduplicare.
6. Confirmă dacă fiecare domeniu are coș separat și dacă autentificările sunt comune. Activează pregătirea coșului doar după validarea DOM-ului real. Păstrează CAPTCHA și 3-D Secure manuale și nu automatiza un checkout de țară până când plafonul său nativ și toate taxele sunt confirmate.

## De ce nu rulează workerii pe Vercel

Vercel găzduiește numai pagina statică de prezentare. Workerii, Playwright-ul cu profil persistent, setările și SQLite rămân pe calculatorul Windows al utilizatorului; astfel, sesiunile de cont și coșurile nu sunt trimise la hosting.
