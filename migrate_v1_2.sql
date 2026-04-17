-- v1.2 migration: vessels tablosuna yeni alanlar
ALTER TABLE vessels ADD COLUMN acenta TEXT;
ALTER TABLE vessels ADD COLUMN tug_var INTEGER DEFAULT 0;
ALTER TABLE vessels ADD COLUMN tug_adet INTEGER DEFAULT 0;
ALTER TABLE vessels ADD COLUMN process TEXT;
