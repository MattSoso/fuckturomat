# CreativeCrafts Faktury 0.7 — Windows

Rozpakuj całą paczkę i uruchom URUCHOM.bat. Wymagany Python 3.9.2+ (testowano 3.12); pierwsze uruchomienie instaluje reportlab, lxml i cryptography w prywatnym .venv. To paczka źródłowa z launcherem BAT, bez instalatora EXE. START_WINDOWS.html zawiera instrukcję.

## Aktualizacja

W poprzedniej wersji pobierz pełną kopię ZIP. Zamknij czarne okno programu, rozpakuj nową paczkę do nowego folderu, uruchom URUCHOM.bat. Dane, dokumenty, kontrahenci, numery i lokalne rozliczenia pozostają w tej samej bazie %LOCALAPPDATA%\CreativeCraftsFaktury\faktury.sqlite3. Schemat jest rozszerzany bez kasowania wcześniejszych tabel. Nie uruchamiaj dwóch wersji równolegle. Klucz Base wpisz raz i zapisz lokalnie.

## Faktury B2C i B2B

Uzupełnij sprzedawcę i numerację. Użyj osobnego prefiksu, aby nie kolidować z Fakturownią. Najpierw sprawdź podgląd. W ustawieniach włącz wystawianie po sprawdzeniu danych.

- NIP nabywcy pusty: B2C. Polski NIP: B2B, sprawdzany checksum.
- Krajowe zwykłe faktury VAT w PLN, stawki 0/5/8/23%. Nie obsługujemy transakcji zagranicznych, OSS, zwolnienia, marży, MPP, odwrotnego obciążenia, metody kasowej, samofakturowania i zaliczek. Nie stosuj tego formularza do takich przypadków.
- Dokumenty nowe mają dzisiejszą datę. Numeracja roczna PREFIKS/0001/ROK, nadawana transakcyjnie; niepowodzenie generowania XML/PDF nie zużywa numeru. Podgląd nie nadaje numeru.
- Obliczenia Decimal dla danych z Base: brutto pozycji = jednostkowa cena brutto × ilość, zaokrąglenie do groszy; netto = brutto / (1 + stawka VAT), zaokrąglenie do groszy; VAT = różnica. Sumowane są wartości pozycji.
- Przygotowana B2B otrzymuje lokalny numer oraz XML, ale pozostaje oznaczona jako niepotwierdzona w KSeF. PDF przed potwierdzeniem nie służy jako potwierdzenie wystawienia w KSeF.
- Formularz obejmuje jednostki, płatność, zapłaconą kwotę i datę, kontakty, uwagi, bank. Brak daty zapłaty nie jest zgadywany: XML pomija wtedy opcjonalną informację o dokonanej zapłacie, a panel pokazuje ostrzeżenie. PDF nadal prezentuje zapisaną kwotę.

## Automatyczny KSeF — wersja 0.7

1. W Ustawieniach → Połączenie z KSeF wybierz test/demo/produkcję i wpisz token KSeF odpowiedniego środowiska. Wymagane uprawnienie do wystawiania faktur (InvoiceWrite) dla NIP sprzedawcy. Token Base jest osobnym kluczem.
2. Zapisz ustawienia, następnie wybierz „Sprawdź połączenie”. To sprawdza uwierzytelnienie, nie wystawia faktury i nie potwierdza wszystkich uprawnień do wysyłki.
3. Zaznacz automat dla NOWYCH faktur B2B. Produkcja wymaga dodatkowego zaznaczenia świadomej wysyłki rzeczywistych faktur. Domyślnie test i wyłączony automat. Nie włączaj produkcji przed testem całego obiegu.
4. Nowa faktura/korekta B2B zostaje zapisana razem ze zleceniem wysyłki w jednej transakcji. Program co 30 sekund obsługuje kolejkę. Musi być uruchomiony i mieć internet. Dotychczasowe dokumenty, archiwum oraz B2C nie są automatycznie wysyłane. Przycisk „Wyślij do KSeF” pozwala zlecić konkretny bieżący dokument.
5. Szczegóły pokazują środowisko, stan, referencje i komunikat KSeF. Kod 200 i zgodny SHA256 dokumentu pozwalają zapisać numer KSeF oraz PDF z QR. UPO jest pobierane i trafia do kopii ZIP. „Przyjęta — oczekuje na UPO” oznacza, że pobranie UPO będzie ponowione; nie oznacza ponownej wysyłki faktury.

Token KSeF zapisywany jest w osobnym, NIESZYFROWANYM pliku ksef-settings.json w folderze danych. Nie trafia do kopii ani odpowiedzi panelu. Nie udostępniaj całego folderu danych; chroń konto systemowe i dysk. Puste pole tokenu zachowuje zapisany klucz tylko w tym samym środowisku. Zmiana środowiska wymaga innego tokenu. Usunięcie tokenu wyłącza automat; kolejka czeka na właściwy klucz. Wyłączenie automatu nie odwołuje już zapisanych zleceń. Przed przywracaniem starej kopii uzgodnij stan z KSeF — kopia może nie zawierać późniejszych wysyłek.

Wysyłka obsługuje ONLINE z dzisiejszym P_1. Zmiana daty przed pierwszą wysyłką blokuje dokument. Brak offline24, certyfikatów offline i drugiego QR. Jedna faktura ma jedną próbę API w jednym środowisku; nie przenosimy potwierdzonej testowej faktury na produkcję. Dla testów używaj osobnej bazy/prefiksu i danych testowych.

Po przerwaniu wysyłki program sprawdza sesję po skrócie XML, bez ponownego POST faktury. Gdy wynik pozostaje nieznany, użyj „Sprawdź status” i sprawdź sesję w portalu. Odrzucenia (w tym duplikat 440) nie są oznaczane jako przyjęcie. Nie ma przycisku automatycznego ponowienia odrzuconego dokumentu. Usterki wymagające zmiany XML po próbie API pozostają zablokowane do kontrolowanej obsługi. Gdy zamknięto program podczas otwierania sesji, dokument również wymaga kontroli. Nie wystawiaj kolejnego numeru w celu obejścia błędu.

## Naprawa XML z wersji 0.6

Generator wpisywał razem P_11 (wartość netto) i P_11A (wartość brutto). XSD dopuszcza oba opcjonalne elementy, ale walidator Aplikacji Podatnika zgłosił ich konflikt. Wersja 0.7 dopasowuje pola do sposobu obliczania pozycji: P_9A + P_11 dla netto albo P_9B + P_11A dla brutto. P_11Vat zachowuje VAT pozycji; P_15 sumę brutto faktury. Kwoty, numer i daty nie są przeliczane w naprawie.

Dla już zapisanej, odrzuconej faktury otwórz szczegóły, potwierdź brak przyjęcia w KSeF i kliknij „Napraw konflikt netto/brutto w XML”. Pobierz XML ponownie. Zachowujemy wcześniejsze bajty w bazie i kopii ZIP. Dokumenty potwierdzone lub z próbą API nie podlegają tej naprawie. To ważne: kod QR musi odnosić się do dokładnie tego pliku, który przyjął KSeF, więc nie potwierdzaj w programie ręcznie zmienionego zewnętrznego XML bez zgodnej aktualizacji zapisu.

## XML FA(3) i ręczny KSeF

1. Przygotuj nową fakturę lub korektę. Otwórz „Moje faktury” → „Szczegóły / płatność”.
2. Kliknij „Pobierz XML FA(3)”. To eksport do pliku, a nie wysyłka przez API. XML jest sprawdzany oficjalnym schematem XSD FA(3) wraz z lokalnymi zależnościami; sama walidacja nie potwierdza akceptacji KSeF ani prawidłowości podatkowej.
3. Najpierw przetestuj import XML w środowisku testowym Aplikacji Podatnika KSeF. Dane testowe i numer testowy nie oznaczają wystawienia faktury produkcyjnej. Dla rzeczywistego wystawienia przygotuj osobny prawidłowy dokument i prześlij go w środowisku produkcyjnym.
4. Obsługiwany scenariusz: ręczne przesłanie ONLINE w dniu przygotowania (P_1). Nie używaj tego procesu jako offline24/awarii/offline — brakuje certyfikatów i drugiego kodu QR. Jeżeli nie wysłałeś dokumentu w tym dniu lub KSeF go odrzucił, sprawdź właściwy tryb i komunikat w KSeF; nie oznaczaj odrzuconej faktury jako przyjętej. Dostępna jest naprawa konfliktu P_11/P_11A w starym XML, jeśli potwierdzisz, że faktura nie została przyjęta i nie ma próby API. Pozostałe błędy wymagają analizy; nie ma ogólnego edytora odrzuconych faktur. Nie obchodź tego wystawieniem duplikatu.
5. Po przyjęciu TEGO XML wpisz otrzymany numer KSeF, wybierz właściwe środowisko i zaznacz potwierdzenie przyjęcia. Program sprawdza format, datę w numerze i NIP sprzedawcy. Nie sprawdza sumy kontrolnej numeru, statusu serwera ani zgodności UPO — jest to Twoje ręczne potwierdzenie, nie weryfikacja API.
6. Po potwierdzeniu PDF otrzymuje numer KSeF i jeden kod QR oparty na SHA256 dokładnie zapisanego XML, dacie P_1 i NIP. Test/demo są oznaczone jako testowe i blokują przekazanie B2B do Base. Numer i środowisko nie mogą być zastąpione po zapisie. UPO pobierz z KSeF i przechowuj osobno.

XML jest zapisywany raz, nie odtwarzany przy każdym pobraniu. Późniejsze wpłaty nie zmieniają XML ani jego skrótu. Pierwotna wersja przygotowanego PDF/JSON jest zachowana w bazie i kopii ZIP; po potwierdzeniu pobierany PDF jest wizualizacją z numerem KSeF/QR. Starsze dokumenty programu oraz archiwum Fakturowni nie dostają automatycznie nowego XML — nie należy wystawiać ich ponownie w KSeF.

Program nie określa indywidualnego obowiązku korzystania z KSeF. B2C ma XML technicznie dostępny, ale jego pobranie nie nakazuje wysłania faktury konsumenckiej do KSeF.

## Korekty

Otwórz szczegóły faktury pierwotnej → „Wystaw korektę”. Obsługiwane są korekty ilości, ceny, nazwy/jednostki i stawki VAT. Formularz pokazuje stan przed; wpisujesz stan po. Ilość 0 oznacza pełny zwrot pozycji. Nie zmienia liczby pozycji, nabywcy ani sprzedawcy.

Podaj przyczynę i termin rozliczenia. Dla pierwotnej faktury w KSeF podaj jej numer KSeF; dla dokumentu spoza KSeF zaznacz wyraźne potwierdzenie tego faktu. Brak numeru w archiwum nie jest automatycznie traktowany jako brak wystawienia w KSeF. Zapisany numer lokalnej faktury nie może być zmieniony. Korekta w KSeF musi być potwierdzona w tym samym środowisku co lokalna faktura pierwotna.

Numeracja osobna: PREFIKS-KOR/0001/ROK. Kwoty netto, VAT i brutto korekty są RÓŻNICĄ pomiędzy stanem po a stanem przed. Pozycje w szczegółach przedstawiają stan po; PDF przedstawia oba stany. XML używa par wierszy „StanPrzed=1” / „po” z tym samym numerem wiersza. Faktura pierwotna, jej numer i PDF nie są nadpisywane.

Kolejną korektę otwieraj zawsze z faktury pierwotnej: stan przed pochodzi z ostatniej lokalnej korekty. Poprzednia B2B musi najpierw mieć zapisany numer KSeF. Program chroni przed zapisem drugiej korekty ze starego stanu. Nie uwzględnia automatycznie korekt wystawionych w innych systemach — jeżeli takie istnieją, nie korzystaj z tej funkcji do dalszej korekty bez uzgodnienia stanu.

Można korygować własne krajowe zwykłe faktury sprzedaży VAT pobrane z Fakturowni. NIP sprzedawcy musi zgadzać się z ustawieniami. Sumy źródłowe muszą zgadzać się z sumą pozycji. Niezmienione pozycje zachowują oryginalne kwoty nawet przy różnicy ceny jednostkowej i sumy (np. 15.01 / 15.00). Archiwum nie jest przeliczane ani edytowane. Nie obsługujemy korekt kosztów, proform, zaliczek, zmiany sprzedawcy/nabywcy ani korekt wystawionych poza tym programem.

Korekty do Base przekazujesz na razie ręcznie. Program nie podmienia korektą PDF w pierwotnym rekordzie Base. Nie ma automatycznego księgowania zwrotów ani pomniejszania salda faktury pierwotnej; ujemne korekty mają zablokowane wpisywanie dodatniej wpłaty. Kwotę korekty rozlicz oddzielnie.

## Base

Klucz wpisuj w panelu; zostanie zapisany w osobnym pliku lokalnym. Pobieranie zamówień nie modyfikuje Base. Lista pokazuje ostatnie 1000 zapisanych zamówień; synchronizacja ma limit 10 000 na operację. Wymagany istniejący nowy zwykły rekord faktury, bez zewnętrznego numeru, powiązania Fakturowni lub statusu KSeF. Dane muszą być krajowe w PLN, daty aktualne, a sumy zgodne z Base.

Przygotuj fakturę z zamówienia. Dla B2B otwórz szczegóły, prześlij XML do produkcyjnego KSeF przez API lub ręcznie. Po potwierdzeniu przyjęcia przekaż PDF do Base. Program przekazuje plik i numer handlowy przez addOrderInvoiceFile; nie aktualizuje pól gov_id/gov_status w Base i nie wysyła bezpośrednio na Allegro. Dystrybucję nadal wykonujesz w Base. Sprawdź wymagania swojej integracji i widoczność finalnego PDF.

Powiązanie z zamówieniem jest unikalne. Ponowienie wysyłki używa tego samego numeru/PDF. Zmiana danych rekordu Base, inny dokument zewnętrzny lub status KSeF zatrzymuje wysyłkę. Nie uruchamiaj równolegle automatycznego wystawiania przez Fakturownię dla tego samego zamówienia.

## Moje faktury i Lista kontrahentów — wersja 0.5

Usunięto zakładkę importu, formularz tokenu i integrację API Fakturowni. Program nie łączy się już z Fakturownią. Wcześniej pobrane dane pozostają w lokalnej bazie, razem z oryginalnymi PDF/JSON, załącznikami i raportami w kopii ZIP.

„Moje faktury” to wspólna lista wszystkich nowych i wcześniejszych dokumentów: sprzedaży, kosztów i korekt. Dostępne wyszukiwanie numeru, kontrahenta i NIP oraz filtry płatności, rodzaju i źródła. Lista jest stronicowana po 50 rekordów — można przeglądać cały zbiór. Dokumenty sortowane są od najnowszej daty wystawienia. Licznik na pulpicie uwzględnia wszystkie dokumenty.

Dokumenty historyczne zachowują stare numery i pliki. Nowe nadal otrzymują numery lokalne. Lista nie wystawia archiwum ponownie i nie podmienia wcześniejszych plików. To samo ID w dwóch źródłach nie powoduje nadpisania: źródło dokumentu jest zachowane. Dla kosztów na liście widoczny jest dostawca; szczegóły pokazują oryginalnego sprzedawcę i nabywcę. Rozliczenia, korekty, PDF i XML nowych dokumentów są nadal dostępne ze szczegółów. PDF/JSON oraz pobrane załączniki dokumentów historycznych są dostępne bez połączenia sieciowego. Brak pobranego PDF jest oznaczony — ta wersja nie pobiera brakujących plików z Fakturowni.

„Lista kontrahentów” zawiera wcześniej pobranych kontrahentów i nabywców faktur. Przy pierwszym uruchomieniu wersji 0.5 jest uzupełniana automatycznie. Firma z tym samym NIP i krajem ma jeden wpis; osoby łączone są po nazwie z pełnym adresem lub nazwie z e-mailem. Samo podobne imię i nazwisko nie powoduje łączenia niepełnych danych. Oryginalne rekordy importu są zachowane.

Dostępne dane: nazwa, NIP, ulica, kod pocztowy, miejscowość, kraj, e-mail i telefon. Użyj „+ Dodaj kontrahenta”, aby dodać nową osobę lub firmę. „Edytuj” zmienia lokalną kartotekę; nie zmienia nabywcy na starych fakturach. NIP krajowej firmy jest sprawdzany. Przy duplikacie program wskazuje istniejący wpis; przy równoczesnej edycji chroni przed nadpisaniem nowszej zmiany.

„Użyj do faktury” wypełnia nabywcę w formularzu. Program wystawia tylko krajowe dokumenty; kontakt zagraniczny można zapisać na liście, lecz nie użyć do wystawienia w tej wersji. Nowy nabywca trafia na listę automatycznie dopiero przy zapisaniu faktury (również z Base). Podgląd nie tworzy kontrahenta. Przy istniejącym wpisie faktura może uzupełnić puste pola, ale nie zastępuje ręcznie poprawionych danych. Ponowne uruchomienie nie nadpisuje edytowanej kartoteki danymi historycznymi.

Pełna kopia ZIP dodatkowo zawiera kontrahenci/lista.json i kartotekę w bazie. Aktualizacja nie wymaga ponownego pobierania ani przepisywania danych. Zachowaj kopię ze starej wersji przed aktualizacją; nie uruchamiaj dwóch wersji równolegle.

## Rozliczenia

Szczegóły lokalne i archiwalne pokazują kwotę, zapłatę, pozostało, termin, metodę, datę i historię. Wpisuj łączną zapłaconą kwotę, nie kolejną wpłatę. „Oznacz jako opłaconą” proponuje kwotę i dzisiejszą datę; sprawdź dane i zapisz. Historia uwzględnia zmniejszenie kwoty. Brak kwoty/dat w źródle nie jest zgadywany. Po terminie oznacza dodatnie saldo i termin wcześniejszy niż dziś.

Zmiany rozliczeń są lokalne, chronione wersją przed nadpisaniem z innego okna i zachowywane po aktualizacji. Nie aktualizują Base, nie regenerują PDF ani XML i nie wysyłają maili. PDF nowej faktury przedstawia wpłatę znaną przy przygotowaniu.

## Dane i kopie

Baza: Windows %LOCALAPPDATA%\CreativeCraftsFaktury\faktury.sqlite3; Mac ~/Library/Application Support/CreativeCraftsFaktury/faktury.sqlite3. Nie synchronizuj działającej bazy SQLite przez chmurę.

Pełna kopia ZIP zawiera bazę, PDF/JSON nowych faktur, zapisane XML FA(3), przygotowane wersje PDF/JSON sprzed potwierdzenia KSeF, całe archiwum Fakturowni z manifestami i raportami oraz historię rozliczeń. Zawiera pobrane przez API UPO oraz poprzednie wersje naprawionych XML. Nie zawiera tokenów ani konfiguracji automatu. Nie wykonuje automatycznej kopii na Drive. Kopia nie jest szyfrowana i zawiera dane klientów; przechowuj ją odpowiednio.

Przywracanie: zamknij program, zachowaj obecną bazę, zastąp faktury.sqlite3 plikiem z kopii. Nie wznawiaj pracy ze starej kopii, jeżeli później wystawiono dokumenty — najpierw sprawdź ciągłość numeracji. Nigdy nie uruchamiaj dwóch kopii tej samej numeracji równolegle.

## Sprawdzenie i zakres następnej wersji

60 testów backendu w Linuxie: migracja kartoteki bez nadpisania faktur, jedna lista wszystkich źródeł, wyszukiwanie/filtry/stronicowanie, dodawanie i edycja kontrahenta, automatyczny zapis nabywcy, brak sieciowej integracji Fakturowni, transakcje, numeracja, PDF, archiwum/rozliczenia, schemat FA(3), stawki VAT, korekty dodatnie/ujemne/zerowe, zwroty, historyczne zaokrąglenia, HTTP pobierania XML i ręcznego potwierdzenia, blokada Base dla B2B przed produkcyjnym potwierdzeniem. Sprawdzono składnię JavaScript i wykonanie list faktur/kontrahentów, dodawania/wybierania kontaktu i wcześniejszych formularzy w symulowanym DOM. Nie uruchomiono na rzeczywistym Windowsie ani w pełnej przeglądarce i nie przesłano XML do rzeczywistego KSeF. Testy tekstu PDF mogą używać opcjonalnego pypdf.

W 0.7 dodano testy protokołu z symulowanym serwerem: szyfrowanie AES, uwierzytelnianie tokenem, statusy, UPO, odrzucenia i duplikaty, brak ponownej wysyłki po timeout/restart, naprawa XML oraz brak sekretów w kopii. Do kolejnej wersji pozostają tryby offline i pełna obsługa poprawiania odrzuconych dokumentów API.

Panel nasłuchuje tylko 127.0.0.1 na losowym porcie, wymaga sesji i odrzuca obce pochodzenie zapisów. Brak telemetrii. Połączenia: instalacja zależności, Base na żądanie oraz KSeF dla zleconych dokumentów i przy sprawdzaniu połączenia. Walidacja XML używa lokalnych XSD, bez pobierania schematów w czasie pracy.

## Źródła techniczne

- Schemat FA(3): https://crd.gov.pl/wzor/2025/06/25/13775/schemat.xsd
- Informacje MF: https://ksef.podatki.gov.pl/informacje-ogolne-ksef-20/struktura-logiczna-fa-3/
- Kody QR: https://github.com/CIRFMF/ksef-api/blob/main/kody-qr.md
- Base: https://api.baselinker.com/index.php?method=addOrderInvoiceFile
- Oryginalne archiwum zachowuje dane pobrane we wcześniejszych wersjach; w tej wersji brak API Fakturowni.

## Zmiany 0.6

Nowa faktura przyjmuje jednostkową cenę netto. Wartość netto = ilość × cena netto, zaokrąglona do groszy. VAT = wartość netto × stawka, zaokrąglony do groszy. Brutto = netto + VAT. PDF: nazwa usługi/towaru, ilość, cena netto, wartość netto, VAT %, wartość VAT, wartość brutto. Dokumenty z Base nadal zachowują wcześniejszy sposób obliczeń od brutto. Istniejące PDF-y nie są przepisywane.

W Ustawieniach kliknij Zapisz klucz Base. Program zapisze go w pliku base-key.txt obok bazy danych i wczyta przy kolejnym starcie. Plik nie jest szyfrowany i nie jest dołączany do kopii ZIP. Usuń zapisany klucz usuwa go z pliku oraz bieżącej sesji. Po przeniesieniu na inny komputer lub odtworzeniu kopii wpisz klucz ponownie.
