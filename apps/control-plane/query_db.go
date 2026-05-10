package main

import (
	"fmt"
	"os"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
)

func main() {
	dbURL := "postgresql://postgres.wnhwdwognyvzyrpqtxyy:Itsaliyan2580@aws-1-ap-south-1.pooler.supabase.com:6543/postgres?pgbouncer=true"
	db, err := gorm.Open(postgres.Open(dbURL), &gorm.Config{})
	if err != nil {
		fmt.Println("Error:", err)
		os.Exit(1)
	}

	var count int64
	db.Table("user_profiles").Count(&count)
	fmt.Printf("Total user_profiles: %d\n", count)

	var profiles []map[string]interface{}
	db.Table("user_profiles").Limit(5).Find(&profiles)
	for _, p := range profiles {
		fmt.Printf("User: ID=%v, ClerkID=%v, Email=%v\n", p["id"], p["clerk_user_id"], p["email"])
	}
}
