-- 数据库初始化：启用 pgvector 与 zhparser 扩展，并配置中文分词配置名 'chinese'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS zhparser;

-- 注册 tsvector 中文配置（检索服务使用 plainto_tsquery('chinese', ...)）
DROP TEXT SEARCH CONFIGURATION IF EXISTS chinese;
CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l,j,t,s WITH simple;
