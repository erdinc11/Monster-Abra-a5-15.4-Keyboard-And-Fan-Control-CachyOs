# Abra Kontrol

MONSTER ABRA A5 V15.4 (ITE 8291, `048d:ce00`, denetleyici revizyonu `0.02`) için GTK4 uygulaması.

Klavye sekmesi dört RGB bölgesinde her kanal için doğrudan 0–50 renk seçimi, parlaklık ve yazılımsal animasyonları yönetir. Kompakt fan sekmesi Otomatik, Turbo ve Boost profillerini, sıcaklık ve fan çıkışı bilgisini, ayrıca bu bilgisayarda doğrulanan L1–L3 sabit fan seviyelerini sunar. Arayüz, klavyeyi tuş başına kontrol etmez; sürücü dört bölge sunar.

## Kurulum

Bu bilgisayarda CachyOS kernel başlıkları, `dkms`, `clang`, `acpi_call-dkms`, Python GTK4 ve `video` grup üyeliği gerekir. Kurulum betiği ITE sürücüsünü DKMS'e ekler; sonraki kernel güncellemelerinde yeniden derlenir. Ayrıca fan köprüsü için doğrulanmış profil ve sabit seviye işlemlerini kabul eden bir yönetici yardımcısı kurar. Yardımcı sudoers üzerinden parola penceresi açmadan çalışır.

Terminalden bu klasörde:

```sh
sudo bash setup.sh
```

Uygulamayı menüdeki **Abra Kontrol** kısayolundan veya şöyle başlatabilirsiniz:

```sh
python3 abra_control.py
```

Pencerenin kapat düğmesi uygulamayı KDE sistem tepsisine küçültür. Tepsi simgesine sağ tıklayıp **Abra Kontrol'ü aç** ile pencereyi geri getirebilir, **Kapat** ile uygulamadan tamamen çıkabilirsiniz. **Sistem ile başlat** seçeneği etkinleştirildiğinde oturum açılışında son ayarlar yüklenir ve pencere açılmadan tepside çalışır. Klavye renkleri, bölge parlaklıkları, seçili animasyon ve hızı, fan profili ve diğer tercihler `~/.config/abra-control/settings.json` dosyasına kaydedilir. Animasyon çalışırken çıkış yapılmışsa uygulama yeniden açıldığında animasyon da devam eder.

Kullanıcı `video` grubuna yeni eklendiyse oturumu kapatıp yeniden açın. Tepsiden **Kapat** seçildiğinde varsayılan seçenek açıksa fan Otomatik moda döner; pencereyi tepsiye küçültmek fan ayarını değiştirmez.

## Sürücü kaynağı

`driver/ite_8291.c`, `driver/Kbuild` ve lisans dosyası TUXEDO Computers'ın açık kaynak `tuxedo-drivers` projesinden alınmıştır. Sürücü, bu USB klavye denetleyicisinin `048d:ce00` revizyon `0.02` aygıtını destekler; ABRA A5 V15.4 için renk kanallarındaki genel kısma davranışını kaldıran bir DMI eşlemesi içerir. Kaynak: <https://github.com/tuxedocomputers/tuxedo-drivers>.
